"""Optional, read-only browser discovery for dynamically loaded API calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any


async def discover_api_requests(url: str, *, storage_state: Path | None = None, timeout_ms: int = 30_000, interactions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Observe network responses during a page load when Playwright is installed.

    This intentionally performs navigation only. It does not click controls or
    replay state-changing requests automatically.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("interactive discovery requires the optional 'browser' dependency") from exc

    observations: list[dict[str, Any]] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=str(storage_state) if storage_state else None)
        page = await context.new_page()

        async def record(response: Any) -> None:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type.lower():
                return
            observations.append({"method": response.request.method, "url": response.url, "status": response.status, "content_type": content_type})

        page.on("response", record)
        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        for recipe in interactions or []:
            action = recipe.get("action")
            selector = recipe.get("selector")
            if action not in {"click", "fill", "scroll"}:
                raise ValueError("only click, fill, and scroll interactions are permitted")
            if action != "scroll" and not isinstance(selector, str):
                raise ValueError("interactive selectors are required")
            if action == "click":
                await page.locator(selector).first.click(timeout=timeout_ms)
            elif action == "fill":
                await page.locator(selector).first.fill(str(recipe.get("value", "")))
            else:
                await page.mouse.wheel(0, float(recipe.get("distance", 800)))
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        await browser.close()
    return observations
