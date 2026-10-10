from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, Field


class Observation(BaseModel):
    method: str
    url: str
    path: str
    query: dict[str, str] = Field(default_factory=dict)
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: Any = None
    status: int
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body: Any = None


class Workspace(BaseModel):
    version: str = "1"
    observations: list[Observation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sanitized_values: int = 0
    schemas: dict[str, Any] = Field(default_factory=dict)


SECRET_KEY = re.compile(
    r"(authorization|cookie|set-cookie|password|passwd|token|secret|api[_-]?key|session)",
    re.IGNORECASE,
)
UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
ALLOWED_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)


def sanitize(value: Any, counter: list[int]) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if SECRET_KEY.search(str(k)):
                counter[0] += 1
                out[k] = "<REDACTED>"
            else:
                out[k] = sanitize(v, counter)
        return out
    if isinstance(value, list):
        return [sanitize(v, counter) for v in value]
    if isinstance(value, str):
        patterns = [
            r"Bearer\s+[^\s]+",
            r"(?i)(api[_-]?key|token|password)=([^&\s]+)",
            r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
        ]
        result = value
        for p in patterns:
            new = re.sub(p, lambda m: "<REDACTED>", result)
            if new != result:
                counter[0] += 1
            result = new
        return result
    return value


def _body(text: Any, headers: dict[str, str], limit: int, counter: list[int]) -> Any:
    if not isinstance(text, str) or len(text.encode("utf-8", "ignore")) > limit:
        return None
    clean = sanitize(text, counter)
    ct = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    if "json" in ct:
        try:
            return sanitize(json.loads(clean), counter)
        except (ValueError, TypeError):
            return clean
    return clean


def sanitize_path(path: str, counter: list[int]) -> str:
    """Redact values that follow sensitive URL path labels."""
    parts = path.split("/")
    previous_sensitive = False
    for index, segment in enumerate(parts):
        if previous_sensitive and segment:
            parts[index] = "<REDACTED>"
            counter[0] += 1
        previous_sensitive = bool(SECRET_KEY.search(segment))
        if (
            not previous_sensitive
            and len(segment) >= 24
            and re.fullmatch(r"[A-Za-z0-9._~-]+", segment)
        ):
            parts[index] = "<REDACTED>"
            counter[0] += 1
    return "/".join(parts)


def infer_schema(value: Any) -> Any:
    """Return a deterministic, conservative JSON shape description."""
    if value is None:
        return {"type": ["null"]}
    if isinstance(value, bool):
        return {"type": ["boolean"]}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": ["integer"]}
    if isinstance(value, float):
        return {"type": ["number"]}
    if isinstance(value, str):
        return {"type": ["string"]}
    if isinstance(value, list):
        shapes = [infer_schema(item) for item in value]
        return {"type": ["array"], "items": merge_schemas(shapes)}
    if isinstance(value, dict):
        return {
            "type": ["object"],
            "properties": {str(k): infer_schema(v) for k, v in sorted(value.items())},
        }
    return {"type": ["unknown"]}


def merge_schemas(shapes: list[Any]) -> Any:
    if not shapes:
        return {"type": ["unknown"]}
    types = sorted({t for shape in shapes for t in shape.get("type", ["unknown"])})
    result: dict[str, Any] = {"type": types}
    object_shapes = [shape for shape in shapes if "object" in shape.get("type", [])]
    if object_shapes:
        keys = sorted(
            {k for shape in object_shapes for k in shape.get("properties", {})}
        )
        result["properties"] = {
            k: merge_schemas(
                [
                    s["properties"][k]
                    for s in object_shapes
                    if k in s.get("properties", {})
                ]
            )
            for k in keys
        }
        result["required"] = sorted(
            set.intersection(
                *[set(shape.get("properties", {})) for shape in object_shapes]
            )
        )
    return result


def _safe_output(output: Path) -> None:
    if output.is_symlink():
        raise ValueError("output path may not be a symlink")
    for parent in output.parents:
        if parent == parent.parent:
            break
        if parent.is_symlink():
            raise ValueError("output path may not traverse a symlink")
    if output.exists() and any(output.iterdir()):
        raise ValueError("output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)


def import_har(
    source: Path,
    output: Path,
    max_bytes=25 * 1024 * 1024,
    max_entries=5000,
    body_limit=1024 * 1024,
) -> Workspace:
    if source.stat().st_size > max_bytes:
        raise ValueError("HAR exceeds 25 MiB limit")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        raise ValueError("invalid HAR JSON") from e
    entries = data.get("log", {}).get("entries", [])
    if not isinstance(entries, list):
        raise TypeError("HAR entries must be a list")
    if len(entries) > max_entries:
        raise ValueError("HAR exceeds 5,000 entry limit")
    count = [0]
    observations = []
    warnings = []
    for entry in entries:
        try:
            req, resp = entry["request"], entry["response"]
            url = str(req["url"])
            method = str(req.get("method", "GET")).upper()
            if method not in ALLOWED_METHODS:
                raise ValueError("unsupported HTTP method")
            parts = urlsplit(url)
            path = parts.path or "/"
            rh = {
                str(x.get("name")): str(x.get("value"))
                for x in req.get("headers", [])
                if isinstance(x, dict) and x.get("name")
            }
            sh = {
                str(x.get("name")): str(x.get("value"))
                for x in resp.get("headers", [])
                if isinstance(x, dict) and x.get("name")
            }
            rb = _body(
                req.get("postData", {}).get("text")
                if isinstance(req.get("postData"), dict)
                else None,
                rh,
                body_limit,
                count,
            )
            sb = _body(
                resp.get("content", {}).get("text")
                if isinstance(resp.get("content"), dict)
                else None,
                sh,
                body_limit,
                count,
            )
            query = {
                str(k): str(sanitize(v, count))
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
            }
            if any(ord(ch) < 32 for ch in path):
                raise ValueError("unsafe path")
            clean_path = sanitize_path(path, count)
            observations.append(
                Observation(
                    method=method,
                    url="<ORIGIN>" + clean_path,
                    path=clean_path,
                    query=sanitize(query, count),
                    request_headers=sanitize(rh, count),
                    request_body=rb,
                    status=int(resp.get("status", 200)),
                    response_headers=sanitize(sh, count),
                    response_body=sb,
                )
            )
        except (KeyError, TypeError, ValueError):
            warnings.append("Skipped malformed HAR entry")
    grouped: dict[str, list[Any]] = {}
    for observation in observations:
        if observation.response_body is not None:
            grouped.setdefault(
                f"{observation.method} {route_path(observation.path)}", []
            ).append(infer_schema(observation.response_body))
    schemas = {key: merge_schemas(shapes) for key, shapes in sorted(grouped.items())}
    ws = Workspace(
        observations=observations,
        warnings=warnings,
        sanitized_values=count[0],
        schemas=schemas,
    )
    _safe_output(output)
    (output / "workspace.json").write_text(
        ws.model_dump_json(indent=2), encoding="utf-8"
    )
    return ws


def load_workspace(path: Path) -> Workspace:
    try:
        return Workspace.model_validate_json(
            (path / "workspace.json").read_text(encoding="utf-8")
        )
    except Exception as e:
        raise ValueError("invalid workspace") from e


def route_path(path: str) -> str:
    if any(ord(ch) < 32 or ch in "\r\n" for ch in path):
        raise ValueError("unsafe route path")
    dynamic_index = 0
    result = []
    for part in path.split("/"):
        if part.isdigit() or UUID.match(part):
            dynamic_index += 1
            result.append(
                "{param}" if dynamic_index == 1 else f"{{param{dynamic_index}}}"
            )
        else:
            result.append(part)
    return "/".join(result) or "/"


def validate_workspace(workspace: Workspace) -> list[str]:
    errors: list[str] = []
    if workspace.version != "1":
        errors.append("unsupported workspace version")
    for observation in workspace.observations:
        if observation.method not in ALLOWED_METHODS:
            errors.append("unsupported HTTP method")
        try:
            route_path(observation.path)
        except ValueError:
            errors.append("invalid route path")
        if SECRET_KEY.search(observation.url) and "<REDACTED>" not in observation.url:
            errors.append("sensitive URL data detected")
    for key, schema in workspace.schemas.items():
        if (
            not isinstance(key, str)
            or not isinstance(schema, dict)
            or "type" not in schema
        ):
            errors.append("invalid schema")
    return sorted(set(errors))


def generate(workspace: Workspace, output: Path) -> None:
    _safe_output(output)
    routes = []
    seen = set()
    for o in workspace.observations:
        if o.method not in ALLOWED_METHODS:
            raise ValueError("unsupported HTTP method")
        route = route_path(o.path)
        key = (o.method, route)
        if key in seen:
            continue
        seen.add(key)
        body = (
            json.dumps(o.response_body, ensure_ascii=False)
            if o.response_body is not None
            else "null"
        )
        routes.append((o.method, route, o.status, body))
    route_data = [
        {"method": method, "path": route, "status": status, "body": json.loads(body)}
        for method, route, status, body in routes
    ]
    serialized = json.dumps(route_data, ensure_ascii=False, sort_keys=True)
    lines = [
        "import inspect",
        "import json",
        "from fastapi import FastAPI, Response",
        "app = FastAPI(title='VOAI-GhostAPI mock')",
        f"ROUTES = json.loads({serialized!r})",
        "def _make_handler(body, status, path_names):",
        "    signature = [inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str) for name in path_names]",
        "    def handler():",
        "        return Response(content=json.dumps(body, ensure_ascii=False), status_code=status, media_type='application/json')",
        "    handler.__signature__ = inspect.Signature(signature)",
        "    return handler",
        "for _route in ROUTES:",
        "    _names = [part[1:-1] for part in _route['path'].split('/') if part.startswith('{') and part.endswith('}') ]",
        "    app.add_api_route(_route['path'], _make_handler(_route['body'], _route['status'], _names), methods=[_route['method']])",
    ]
    (output / "app.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (output / "README.md").write_text(
        "# Generated mock\n\nRun `uvicorn app:app --host 127.0.0.1 --port 8000`.\n",
        encoding="utf-8",
    )

