import importlib.util
import json
from pathlib import Path

import pytest
from voai_ghostapi.core import Observation, Workspace, generate, import_har


def har(
    tmp_path: Path, url: str = "https://example.test/items/42?token=SECRET&ok=yes"
) -> Path:
    source = tmp_path / "capture.har"
    source.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {
                                "method": "GET",
                                "url": url,
                                "headers": [
                                    {"name": "Authorization", "value": "Bearer SECRET"}
                                ],
                            },
                            "response": {
                                "status": 200,
                                "headers": [
                                    {
                                        "name": "Content-Type",
                                        "value": "application/json",
                                    }
                                ],
                                "content": {"text": '{"id":42,"name":"demo"}'},
                            },
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return source


def test_import_redacts_query_and_headers(tmp_path):
    ws = import_har(har(tmp_path), tmp_path / "workspace")
    raw = (tmp_path / "workspace" / "workspace.json").read_text()
    assert "SECRET" not in raw
    assert ws.schemas["GET /items/42"]["type"] == ["object"]


def test_generator_escapes_route_source(tmp_path):
    ws = Workspace(
        observations=[
            Observation(
                method="GET",
                url="<ORIGIN>",
                path="/x/'\n",
                status=200,
                response_body={"ok": True},
            )
        ]
    )
    with pytest.raises(ValueError):
        generate(ws, tmp_path / "out")


def test_generator_rejects_overwrite(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "existing").write_text("keep")
    ws = Workspace(
        observations=[
            Observation(
                method="GET", url="<ORIGIN>/x", path="/x", status=200, response_body={}
            )
        ]
    )
    with pytest.raises(ValueError, match="empty"):
        generate(ws, out)


def test_generated_fastapi_route(tmp_path):
    out = tmp_path / "out"
    ws = Workspace(
        observations=[
            Observation(
                method="GET",
                url="<ORIGIN>/items/42",
                path="/items/42",
                status=200,
                response_body={"ok": True},
            )
        ]
    )
    generate(ws, out)
    spec = importlib.util.spec_from_file_location("generated_app", out / "app.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert any(route.path == "/items/{param}" for route in module.app.routes)
    assert module.app.docs_url == "/docs"
    assert "/items/{param}" in module.app.openapi()["paths"]

