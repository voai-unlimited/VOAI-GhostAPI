"""Opt-in live-site smoke coverage for authorized discovery targets."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voai_ghostapi.studio import create_app

LIVE_URL = os.environ.get("VOAI_LIVE_TEST_URL", "https://sauti-omega.vercel.app/")


@pytest.mark.skipif(
    os.environ.get("VOAI_RUN_LIVE_TESTS") != "1",
    reason="live-site tests are opt-in and require VOAI_RUN_LIVE_TESTS=1",
)
def test_authorized_live_site_discovery(tmp_path: Path) -> None:
    """Confirm bounded discovery returns real URLs from the owned live site."""
    client = TestClient(create_app(tmp_path))
    response = client.get("/api/discovery", params={"url": LIVE_URL, "max_pages": 5, "max_depth": 1})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["robots"]["enforced"] is True
    assert payload["robots"]["user_agent"] == "VOAI-GhostAPI-Studio"
    if payload["count"] == 0:
        pytest.skip(f"live target was unreachable or exposed no crawlable URLs: {payload.get('skipped', [])}")
    origin = LIVE_URL.rstrip("/") + "/"
    assert all(item.startswith(origin) for item in payload["urls"])
