import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voai_ghostapi.core import (
    Observation,
    Workspace,
    generate,
    import_har,
    validate_workspace,
)


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
    assert ws.schemas["GET /items/{param}"]["type"] == ["object"]


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


@pytest.mark.parametrize("path", ["/x/'quote", "/x/\\\\backslash", '/x/"quote'])
def test_generated_source_handles_special_paths(tmp_path, path):
    out = tmp_path / "out"
    generate(
        Workspace(
            observations=[
                Observation(
                    method="GET",
                    url="<ORIGIN>",
                    path=path,
                    status=200,
                    response_body={},
                )
            ]
        ),
        out,
    )
    source = (out / "app.py").read_text()
    compile(source, "app.py", "exec")
    assert "__import__" not in source


def test_invalid_method_is_rejected(tmp_path):
    source = har(tmp_path)
    payload = json.loads(source.read_text())
    payload["log"]["entries"][0]["request"]["method"] = "GET;__import__('os')"
    source.write_text(json.dumps(payload))
    workspace = import_har(source, tmp_path / "workspace")
    assert workspace.observations == []
    assert workspace.warnings == ["Skipped malformed HAR entry"]


def test_symlink_output_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="symlink"):
        generate(Workspace(), link)


def test_schema_aggregation_is_deterministic(tmp_path):
    source = har(tmp_path)
    data = json.loads(source.read_text())
    data["log"]["entries"].append(
        {
            **data["log"]["entries"][0],
            "response": {
                **data["log"]["entries"][0]["response"],
                "status": 404,
                "content": {"text": '{"id":null,"extra":true}'},
            },
        }
    )
    source.write_text(json.dumps(data))
    ws = import_har(source, tmp_path / "workspace")
    schema = ws.schemas["GET /items/{param}"]
    assert set(schema["properties"]) == {"id", "name", "extra"}
    assert "null" in schema["properties"]["id"]["type"]


def test_multiple_dynamic_path_parameters_are_distinct(tmp_path):
    if sys.version_info >= (3, 15):
        pytest.skip(
            "local Starlette TestClient compatibility is unavailable on Python 3.15"
        )
    out = tmp_path / "out"
    generate(
        Workspace(
            observations=[
                Observation(
                    method="GET",
                    url="<ORIGIN>",
                    path="/users/1/orders/2",
                    status=200,
                    response_body={},
                )
            ]
        ),
        out,
    )
    spec = importlib.util.spec_from_file_location("multi_app", out / "app.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    operation = module.app.openapi()["paths"]["/users/{param}/orders/{param2}"]["get"]
    assert [item["name"] for item in operation["parameters"]] == ["param", "param2"]
    from fastapi.testclient import TestClient

    with TestClient(module.app) as client:
        response = client.get("/users/1/orders/2")
    assert response.status_code == 200
    assert response.json() == {}


def test_validate_detects_bad_workspace():
    ws = Workspace(
        version="99",
        observations=[
            Observation(
                method="GET", url="https://x.test/?token=SECRET", path="/x", status=200
            )
        ],
    )
    errors = validate_workspace(ws)
    assert "unsupported workspace version" in errors
    assert "sensitive URL data detected" in errors


def test_generated_fastapi_route(tmp_path):
    if sys.version_info >= (3, 15):
        pytest.skip(
            "local Starlette TestClient compatibility is unavailable on Python 3.15"
        )
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

    with TestClient(module.app) as client:
        response = client.get("/items/42")
        unknown = client.get("/does-not-exist")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert unknown.status_code == 404
    operation = module.app.openapi()["paths"]["/items/{param}"]["get"]
    assert all(
        parameter["name"] != "_r" for parameter in operation.get("parameters", [])
    )
    parameter = operation["parameters"][0]
    assert (
        parameter["name"] == "param"
        and parameter["in"] == "path"
        and parameter["required"] is True
    )
