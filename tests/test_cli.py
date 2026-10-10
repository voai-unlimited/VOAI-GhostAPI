from pathlib import Path

import typer
import uvicorn
from rich.console import Console

from . import __version__
from .cleanup import clean_artifacts, find_candidates
from .core import generate as generate_app
from .core import import_har, load_workspace, validate_workspace

app = typer.Typer(
    help="Capture behavior. Reconstruct APIs. Develop offline.",
    invoke_without_command=True,
)
console = Console()


@app.callback()
def main(version: bool = typer.Option(False, "--version", is_flag=True)):
    if version:
        console.print(__version__)
        raise typer.Exit()


@app.command("import")
def import_command(capture: Path, output: Path = typer.Option(..., "--output")):  # noqa: B008
    ws = import_har(capture, output)
    console.print(
        f"Imported {len(ws.observations)} observations; redacted {ws.sanitized_values} values"
    )


@app.command()
def inspect(workspace: Path):
    ws = load_workspace(workspace)
    console.print(f"Observations: {len(ws.observations)}")
    for o in sorted({(x.method, x.path) for x in ws.observations}):
        console.print(f"{o[0]:6} {o[1]}")


@app.command()
def validate(workspace: Path):
    ws = load_workspace(workspace)
    errors = validate_workspace(ws)
    if errors:
        for error in errors:
            console.print(f"[red]Validation error:[/red] {error}")
        raise typer.Exit(code=1)
    console.print(f"Valid workspace: {len(ws.observations)} observations")


@app.command()
def generate(workspace: Path, output: Path = typer.Option(..., "--output")):  # noqa: B008
    generate_app(load_workspace(workspace), output)
    console.print(f"Generated mock in {output}")


@app.command()
def serve(
    generated: Path,
    port: int = typer.Option(8000),
    host: str = typer.Option("127.0.0.1"),
):
    if host not in ("127.0.0.1", "localhost"):
        raise typer.BadParameter("v0.1.0 only permits local binding")
    uvicorn.run("app:app", host=host, port=port, app_dir=str(generated))


@app.command()
def clean(
    all: bool = typer.Option(False, "--all", help="Clean recognized VOAI artifacts."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without deleting."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
    root: Path = typer.Option(".", "--root", file_okay=False),  # noqa: B008
):
    """Safely remove recognized VOAI workspace and generated mock folders."""
    if not all:
        raise typer.BadParameter("--all is required")
    try:
        candidates, skipped = find_candidates(root)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Cleanup error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if candidates:
        console.print("Would remove:")
        for item in candidates:
            console.print(f"  {item.path}")
    else:
        console.print("No recognized VOAI artifacts found.")
    for item in skipped:
        console.print(f"Skipped {item.path}: {item.reason}")
    if dry_run or not candidates:
        return
    if not yes and not typer.confirm("Delete these directories?", default=False):
        console.print("Cleanup cancelled.")
        return
    _, _, errors = clean_artifacts(root)
    for error in errors:
        console.print(f"[red]Cleanup error:[/red] {error}")
    if errors:
        raise typer.Exit(code=1)
    console.print(f"Removed {len(candidates)} artifact director{'y' if len(candidates) == 1 else 'ies'}.")

FILE2
# VOAI-GhostAPI

Capture behavior. Reconstruct APIs. Develop offline.

VOAI-GhostAPI transforms authorized HTTP Archive (HAR 1.2) captures into sanitized, locally runnable FastAPI mock backends. It lets development continue when a remote API is unavailable, unstable, costly, or unsuitable for local testing.

## What is VOAI-GhostAPI?

An offline-first Python tool that imports observations, removes secrets, infers stable endpoint patterns, persists a versioned workspace, and generates deterministic FastAPI fixtures.

## Why VOAI-GhostAPI?

It keeps captured behavior available without contacting the original service. Input is preserved by the caller; imported workspaces contain only sanitized data.

## Features

- HAR 1.2 parsing with size and entry limits
- Stable redaction of credentials, cookies, tokens, and sensitive fields
- Deterministic endpoint and JSON shape inference
- Standalone FastAPI generation with OpenAPI and `/docs`
- Rich CLI and offline operation

## How It Works

```mermaid
flowchart LR
 A[HAR file] --> B[Secure import] --> C[Sanitization] --> D[Endpoint discovery]
 D --> E[Schema inference] --> F[FastAPI mock generation] --> G[Local server]
```

## Requirements

Python 3.11+.

## Installation

```bash
python -m pip install .
```

For a Windows development checkout, create and activate a virtual environment:

```powershell
cd path\to\VOAI-GhostAPI
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
voai-ghostapi --help
voai-ghostapi --version
```

## Quick Start

```powershell
voai-ghostapi import capture.har --output ./workspace
voai-ghostapi inspect ./workspace
voai-ghostapi validate ./workspace
voai-ghostapi generate ./workspace --output ./generated
voai-ghostapi serve ./generated --port 8000
```

To preview and remove recognized generated artifacts beneath the current directory:

```text
voai-ghostapi clean --all --dry-run
voai-ghostapi clean --all
voai-ghostapi clean --all --yes
```

Cleanup only considers recognized `workspace`, `workspace-new`, `workspace-fixed`,
`generated`, `generated-new`, and `generated-fixed` directories. It requires VOAI
markers and skips directories containing unexpected files. HAR captures, virtual
environments, source files, and unrelated files are protected. Stop any running
mock server before cleaning its generated directory.

The import command expects a HAR file in the current project folder. For example, save an authorized browser capture as `capture.har`, then run the commands above. Open `http://127.0.0.1:8000/docs` to explore the generated mock.

## CLI Commands

`import` sanitizes a HAR into a workspace; `inspect` summarizes observations; `validate` checks its version and records; `generate` writes a runnable app; `serve` runs it on `127.0.0.1`.

## Example Workflow

Export a synthetic or authorized capture, import it, inspect the discovered methods, validate the workspace, generate fixtures, then browse `http://127.0.0.1:8000/docs`.

## Security and Privacy

Credentials, cookies, bearer tokens, API keys, passwords, and common personal data are replaced before persistence. Original-to-placeholder mappings are never stored. The importer never executes captured content or makes network requests. Automated redaction cannot identify every proprietary secret; review sanitized output before sharing.

## Project Architecture

The `src/voai_ghostapi` package separates models, parsing, sanitization, inference, persistence, generation, runtime, and CLI concerns.

## Supported Formats

HAR 1.2 JSON with textual request and response bodies. Binary, malformed, oversized, and unsupported bodies are skipped safely.

## Current Development Status

Version 0.1.0 local implementation is complete and testable. GitHub publication is pending installation of Git and GitHub CLI authentication as `voai-unlimited`.

## Limitations

Mocks replay observed examples and do not infer business logic, state, authentication, or unobserved behavior. Dynamic routes are inferred only for numeric and UUID path segments.

## Development Roadmap

See [docs/ROADMAP.md](docs/ROADMAP.md). Future releases cover stateful scenarios, SDKs, a dashboard, and optional offline AI analysis.

## Contributing

See `CONTRIBUTING.md` for local checks and the contribution process.

## License

MIT.

FILE3
import json
from pathlib import Path

from typer.testing import CliRunner

from voai_ghostapi.cli import app

runner = CliRunner()


def make_har(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {
                                "method": "GET",
                                "url": "https://example.test/items/42",
                                "headers": [],
                            },
                            "response": {
                                "status": 200,
                                "headers": [
                                    {
                                        "name": "Content-Type",
                                        "value": "application/json",
                                    }
                                ],
                                "content": {"text": '{"ok":true}'},
                            },
                        }
                    ]
                }
            }
        )
    )


def test_cli_help_and_version():
    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert runner.invoke(app, ["--version"]).stdout.strip() == "0.1.0"


def test_cli_complete_workflow(tmp_path):
    capture = tmp_path / "capture.har"
    make_har(capture)
    workspace = tmp_path / "workspace"
    generated = tmp_path / "generated"
    assert (
        runner.invoke(
            app, ["import", str(capture), "--output", str(workspace)]
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["inspect", str(workspace)]).exit_code == 0
    assert runner.invoke(app, ["validate", str(workspace)]).exit_code == 0
    assert (
        runner.invoke(
            app, ["generate", str(workspace), "--output", str(generated)]
        ).exit_code
        == 0
    )
    assert (generated / "app.py").exists()


def test_cli_failures_have_nonzero_exit(tmp_path):
    missing = runner.invoke(app, ["inspect", str(tmp_path / "missing")])
    assert missing.exit_code != 0
    bad = tmp_path / "bad.har"
    bad.write_text("not-json")
    assert (
        runner.invoke(
            app, ["import", str(bad), "--output", str(tmp_path / "workspace")]
        ).exit_code
        != 0
    )
    invalid = tmp_path / "workspace"
    invalid.mkdir()
    (invalid / "workspace.json").write_text('{"version":"bad","observations":[]}')
    assert runner.invoke(app, ["validate", str(invalid)]).exit_code == 1


def test_cli_serve_rejects_nonlocal_host(tmp_path):
    result = runner.invoke(app, ["serve", str(tmp_path), "--host", "0.0.0.0"])
    assert result.exit_code != 0


def _make_generated(path: Path) -> None:
    path.mkdir()
    (path / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI(title='VOAI-GhostAPI mock')\n")
    (path / "README.md").write_text("# Generated mock\n")


def test_clean_dry_run_and_yes_protects_unrelated_files(tmp_path):
    workspace = tmp_path / "workspace-new"
    workspace.mkdir()
    (workspace / "workspace.json").write_text('{"version":"1","observations":[]}')
    generated = tmp_path / "generated-new"
    _make_generated(generated)
    (tmp_path / "capture.har").write_text("HAR")
    (tmp_path / "source.py").write_text("print('keep')")
    result = runner.invoke(app, ["clean", "--all", "--dry-run", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert workspace.exists() and generated.exists()
    result = runner.invoke(app, ["clean", "--all", "--yes", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert not workspace.exists() and not generated.exists()
    assert (tmp_path / "capture.har").exists() and (tmp_path / "source.py").exists()


def test_clean_confirmation_and_unknown_files_are_safe(tmp_path):
    workspace = tmp_path / "workspace-fixed"
    workspace.mkdir()
    (workspace / "workspace.json").write_text('{"version":"1","observations":[]}')
    (workspace / "notes.txt").write_text("keep")
    result = runner.invoke(app, ["clean", "--all", "--root", str(tmp_path)], input="n\n")
    assert result.exit_code == 0
    assert workspace.exists()
    assert "unexpected user files" in result.stdout


def test_clean_rejects_symlink_and_missing_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "workspace.json").write_text('{"version":"1","observations":[]}')
    link = tmp_path / "workspace"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    result = runner.invoke(app, ["clean", "--all", "--yes", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert target.exists()
    result = runner.invoke(app, ["clean", "--all", "--root", str(tmp_path / "nope")])
    assert result.exit_code == 1

