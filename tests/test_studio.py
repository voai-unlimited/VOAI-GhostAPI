from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

import voai_ghostapi.studio as studio_module
from voai_ghostapi.studio import create_app


def _har() -> bytes:
    return json.dumps(
        {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": {"method": "GET", "url": "https://example.test/api/items/1", "headers": []},
                        "response": {
                            "status": 200,
                            "headers": [{"name": "content-type", "value": "application/json"}],
                            "content": {"mimeType": "application/json", "text": '{"id": 1, "name": "demo"}'},
                        },
                    }
                ],
            }
        }
    ).encode()


def test_studio_serves_dashboard_and_enforces_confirmation(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    assert client.get("/").status_code == 200
    assert "Extraction Studio" in client.get("/").text
    assert client.get("/assets/styles.css").status_code == 200
    response = client.post("/api/jobs", json={"mode": "har"})
    assert response.status_code == 400
    assert "confirmation" in response.json()["detail"]


def test_studio_rejects_nonlocal_host_and_origin(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    assert client.get("/api/health", headers={"host": "example.test"}).status_code == 400
    assert client.get(
        "/api/health", headers={"origin": "https://example.test"}
    ).status_code == 403


def test_studio_runs_har_job_and_exposes_inventory(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    payload = {
        "mode": "har",
        "output": "project-one",
        "confirmed": True,
        "har_uploads": [{"name": "capture.har", "data_base64": base64.b64encode(_har()).decode()}],
    }
    response = client.post("/api/jobs", json=payload)
    assert response.status_code == 200, response.text
    job_id = response.json()["id"]
    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.02)
    assert job["status"] == "completed", job
    assert not (tmp_path / ".studio-uploads").exists()
    stream = client.get(f"/api/jobs/{job_id}/events")
    assert stream.status_code == 200
    assert "complete" in stream.text
    projects = client.get("/api/projects").json()
    assert projects[0]["name"] == "project-one"
    apis = client.get("/api/projects/project-one/apis").json()
    assert apis["observations"][0]["path"] == "/api/items/1"
    generated = client.post("/api/projects/project-one/mock")
    assert generated.status_code == 200, generated.text
    assert generated.json()["status"] == "ready"
    started = client.post("/api/projects/project-one/mock/start", json={"port": 8766})
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "running"
    stopped = client.post("/api/projects/project-one/mock/stop")
    assert stopped.status_code == 200
    resources = client.get("/api/projects/project-one/resources").json()
    assert any(item["path"] == "workspace/workspace.json" for item in resources)
    refused_delete = client.request("DELETE", "/api/projects/project-one", json={"confirmed": False})
    assert refused_delete.status_code == 400
    deleted = client.request("DELETE", "/api/projects/project-one", json={"confirmed": True})
    assert deleted.status_code == 200, deleted.text
    assert client.get("/api/projects").json() == []


def test_studio_rejects_invalid_mode_inputs_and_path_escape(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    website_without_url = client.post("/api/jobs", json={"mode": "website", "confirmed": True})
    assert website_without_url.status_code == 400
    combined_without_har = client.post("/api/jobs", json={"mode": "combined", "url": "https://example.test", "confirmed": True})
    assert combined_without_har.status_code == 400
    escaped = client.post(
        "/api/jobs",
        json={
            "mode": "har",
            "output": "../outside",
            "confirmed": True,
            "har_uploads": [{"name": "capture.har", "data_base64": base64.b64encode(_har()).decode()}],
        },
    )
    assert escaped.status_code == 400


def test_studio_enforces_scope_and_serves_snapshot_safely(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_extract(*args, **kwargs):
        captured.update(kwargs)
        output = Path(args[1])
        output.mkdir(parents=True, exist_ok=True)
        (output / "extraction-report.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(studio_module, "extract", fake_extract)
    client = TestClient(create_app(tmp_path))
    missing_pages = client.post(
        "/api/jobs", json={"mode": "website", "url": "https://example.test", "scope": "selected", "confirmed": True}
    )
    assert missing_pages.status_code == 400

    response = client.post(
        "/api/jobs",
        json={"mode": "website", "url": "https://example.test", "scope": "page", "confirmed": True},
    )
    assert response.status_code == 200
    job_id = response.json()["id"]
    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.02)
    assert job["status"] == "completed", job
    assert captured["max_pages"] == 1
    assert captured["max_depth"] == 0

    project = tmp_path / "snapshot-project"
    (project / "snapshot" / "resources").mkdir(parents=True)
    (project / "extraction-report.json").write_text("{}", encoding="utf-8")
    (project / "snapshot" / "snapshot.json").write_text('{"files": 1}', encoding="utf-8")
    (project / "snapshot" / "resources" / "index.html").write_text("<h1>offline</h1>", encoding="utf-8")
    preview = client.get("/api/projects/snapshot-project/snapshot")
    assert preview.status_code == 200
    assert "offline" in preview.text
    assert preview.headers["content-security-policy"].startswith("default-src 'none'")


