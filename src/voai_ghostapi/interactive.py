"""Optional, read-only browser discovery for dynamically loaded API calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_SAFE_ACTIONS = frozenset({"click", "fill", "select", "scroll"})
_MAX_INTERACTIONS = 50


async def discover_api_requests(url: str, *, storage_state: Path | None = None, timeout_ms: int = 30_000, interactions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Observe network responses during a page load when Playwright is installed.

    This intentionally performs navigation only. It does not click controls or
    replay state-changing requests automatically.
    """
    if interactions is not None and len(interactions) > _MAX_INTERACTIONS:
        raise ValueError(f"at most {_MAX_INTERACTIONS} interactive actions are permitted")
    prepared: list[tuple[str, str | None, str, str, int, float]] = []
    for index, recipe in enumerate(interactions or [], start=1):
        action = recipe.get("action")
        selector = recipe.get("selector")
        if action not in _SAFE_ACTIONS:
            raise ValueError("only click, fill, select, and scroll interactions are permitted")
        if action in {"click", "fill", "select"} and not isinstance(selector, str):
            raise ValueError("interactive selectors are required")
        repeat = recipe.get("repeat", 1)
        if not isinstance(repeat, int) or not 1 <= repeat <= 10:
            raise ValueError("interaction repeat must be an integer from 1 to 10")
        distance = float(recipe.get("distance", 800))
        if action == "scroll" and not -100_000 <= distance <= 100_000:
            raise ValueError("scroll distance is out of bounds")
        prepared.append((str(action), selector if isinstance(selector, str) else None, str(recipe.get("name") or f"{action}-{index}"), str(recipe.get("value", "")), repeat, distance))

    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("interactive discovery requires the optional 'browser' dependency") from exc

    if storage_state is not None and (not storage_state.is_file() or storage_state.is_symlink()):
        raise ValueError("storage state must be a regular local file")

    observations: list[dict[str, Any]] = []
    active_interaction: str | None = None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(storage_state=str(storage_state) if storage_state else None)
            page = await context.new_page()

            async def record(response: Any) -> None:
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type.lower():
                    return
                item: dict[str, Any] = {"method": response.request.method, "url": response.url, "status": response.status, "content_type": content_type}
                if active_interaction is not None:
                    item["triggering_interaction"] = active_interaction
                observations.append(item)

            page.on("response", record)
            await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            for action, selector, name, value, repeat, distance in prepared:
                active_interaction = name
                for _ in range(repeat):
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
                    await page.wait_for_load_state("networkidle", timeout=timeout_ms)
                    await page.wait_for_timeout(100)
        finally:
            await browser.close()
    return observations
