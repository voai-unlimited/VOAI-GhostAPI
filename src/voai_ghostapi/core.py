from __future__ import annotations
import json, re
from pathlib import Path
from typing import Any
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

SECRET_KEY = re.compile(r"(authorization|cookie|set-cookie|password|passwd|token|secret|api[_-]?key|session)", re.I)
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)

def sanitize(value: Any, counter: list[int]) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if SECRET_KEY.search(str(k)):
                counter[0] += 1; out[k] = "<REDACTED>"
            else: out[k] = sanitize(v, counter)
        return out
    if isinstance(value, list): return [sanitize(v, counter) for v in value]
    if isinstance(value, str):
        patterns = [r"Bearer\s+[^\s]+", r"(?i)(api[_-]?key|token|password)=([^&\s]+)", r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"]
        result = value
        for p in patterns:
            new = re.sub(p, lambda m: "<REDACTED>", result)
            if new != result: counter[0] += 1
            result = new
        return result
    return value

def _body(text: Any, headers: dict[str, str], limit: int, counter: list[int]) -> Any:
    if not isinstance(text, str) or len(text.encode("utf-8", "ignore")) > limit: return None
    clean = sanitize(text, counter)
    ct = next((v for k,v in headers.items() if k.lower()=="content-type"), "")
    if "json" in ct:
        try: return sanitize(json.loads(clean), counter)
        except (ValueError, TypeError): return clean
    return clean

def import_har(source: Path, output: Path, max_bytes=25*1024*1024, max_entries=5000, body_limit=1024*1024) -> Workspace:
    if source.stat().st_size > max_bytes: raise ValueError("HAR exceeds 25 MiB limit")
    try: data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as e: raise ValueError("invalid HAR JSON") from e
    entries = data.get("log", {}).get("entries", [])
    if not isinstance(entries, list): raise ValueError("HAR entries must be a list")
    if len(entries) > max_entries: raise ValueError("HAR exceeds 5,000 entry limit")
    count=[0]; observations=[]; warnings=[]
    for entry in entries:
        try:
            req, resp = entry["request"], entry["response"]; url=str(req["url"]); method=str(req.get("method","GET")).upper()
            from urllib.parse import urlsplit, parse_qsl
            parts=urlsplit(url); path=parts.path or "/"
            rh={str(x.get("name")): str(x.get("value")) for x in req.get("headers",[]) if isinstance(x,dict) and x.get("name")}
            sh={str(x.get("name")): str(x.get("value")) for x in resp.get("headers",[]) if isinstance(x,dict) and x.get("name")}
            rb=_body(req.get("postData",{}).get("text") if isinstance(req.get("postData"),dict) else None, rh, body_limit, count)
            sb=_body(resp.get("content",{}).get("text") if isinstance(resp.get("content"),dict) else None, sh, body_limit, count)
            observations.append(Observation(method=method,url="<ORIGIN>"+path,path=path,query=dict(parse_qsl(parts.query)),request_headers=sanitize(rh,count),request_body=rb,status=int(resp.get("status",200)),response_headers=sanitize(sh,count),response_body=sb))
        except (KeyError, TypeError, ValueError): warnings.append("Skipped malformed HAR entry")
    ws=Workspace(observations=observations,warnings=warnings,sanitized_values=count[0]); output.mkdir(parents=True,exist_ok=True); (output/"workspace.json").write_text(ws.model_dump_json(indent=2),encoding="utf-8"); return ws

def load_workspace(path: Path) -> Workspace:
    try: return Workspace.model_validate_json((path/"workspace.json").read_text(encoding="utf-8"))
    except Exception as e: raise ValueError("invalid workspace") from e

def route_path(path: str) -> str:
    return "/".join(f"{{param}}" if (p.isdigit() or UUID.match(p)) else p for p in path.split("/")) or "/"

def generate(workspace: Workspace, output: Path) -> None:
    output.mkdir(parents=True,exist_ok=True); routes=[]; seen=set()
    for o in workspace.observations:
        route=route_path(o.path); key=(o.method,route)
        if key in seen: continue
        seen.add(key); body=json.dumps(o.response_body, ensure_ascii=False) if o.response_body is not None else "null"; routes.append((o.method,route,o.status,body))
    lines=["from fastapi import FastAPI, Response", "app=FastAPI(title='VOAI-GhostAPI mock')"]
    for i,(method,route,status,body) in enumerate(routes):
        fn=f"route_{i}"; lines += [f"@app.{method.lower()}('{route}')", f"def {fn}():", f"    return Response(content={body!r}, status_code={status}, media_type='application/json')"]
    (output/"app.py").write_text("\n".join(lines)+"\n",encoding="utf-8"); (output/"README.md").write_text("# Generated mock\n\nRun `uvicorn app:app --host 127.0.0.1 --port 8000`.\n",encoding="utf-8")

