import json
import subprocess
import sys
from pathlib import Path

import typer
import uvicorn
from rich.console import Console

from . import __version__
from .cleanup import clean_artifacts, find_candidates
from .core import generate as generate_app
from .core import import_har, load_workspace, validate_workspace
from .extract import extract as extract_website
from .interactive import discover_api_requests

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
def preview(
    snapshot: Path,
    port: int = typer.Option(8001),
    host: str = typer.Option("127.0.0.1"),
):
    """Serve an extracted offline snapshot over a local HTTP server."""
    if host not in ("127.0.0.1", "localhost"):
        raise typer.BadParameter("snapshots may only bind locally")
    if not snapshot.is_dir() or not (snapshot / "snapshot.json").is_file():
        raise typer.BadParameter("snapshot must contain snapshot.json")
    subprocess.run([sys.executable, "-m", "http.server", str(port), "--bind", host], cwd=snapshot, check=False)


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
    removed, _, errors = clean_artifacts(root)
    for error in errors:
        console.print(f"[red]Cleanup error:[/red] {error}")
    if errors:
        raise typer.Exit(code=1)
    console.print(f"Removed {len(removed)} artifact director{'y' if len(removed) == 1 else 'ies'}.")


@app.command()
def extract(
    url: str = typer.Option(None, "--url"),
    har: list[Path] = typer.Option([], "--har"),  # noqa: B008
    output: Path = typer.Option(Path("extracted"), "--output"),  # noqa: B008
    yes: bool = typer.Option(False, "--yes"),
    max_pages: int = typer.Option(20, min=1, max=1000),
    max_depth: int = typer.Option(1, min=0, max=20),
    scope: str = typer.Option("site", help="Extraction scope: page or site."),
    resources: str = typer.Option("html,css,js,images,json", help="Comma-separated resource types."),
    snapshot: bool = typer.Option(False, "--snapshot", help="Generate a local website snapshot."),
    max_resource_mb: int = typer.Option(5, min=1, max=100),
    max_total_mb: int = typer.Option(50, min=1, max=1000),
    resume: bool = typer.Option(False, "--resume", help="Resume from the website checkpoint."),
    interactive: bool = typer.Option(False, "--interactive", help="Observe JSON API requests during a read-only browser load."),
    storage_state: Path | None = typer.Option(None, "--storage-state", help="Authorized Playwright storage-state JSON file."),  # noqa: B008
    interactions: Path | None = typer.Option(None, "--interactions", help="JSON list of safe read-only browser interaction recipes."),  # noqa: B008
):
    """Extract a website, HAR files, or both into separate output areas."""
    if not url and not har:
        raise typer.BadParameter("provide --url, --har, or both")
    if scope not in {"page", "site"}:
        raise typer.BadParameter("scope must be page or site")
    if scope == "page":
        max_pages, max_depth = 1, 0
    resource_types = frozenset(item.strip().lower() for item in resources.split(",") if item.strip())
    if not yes and not typer.confirm("Start extraction with the selected inputs?", default=False):
        console.print("Extraction cancelled.")
        return
    try:
        workspace = __import__("asyncio").run(
            extract_website(url or "", output, tuple(har), yes=True, max_pages=max_pages, max_depth=max_depth, resource_types=resource_types, snapshot=snapshot, max_resource_bytes=max_resource_mb * 1024 * 1024, max_total_bytes=max_total_mb * 1024 * 1024, resume=resume)
        )
    except (OSError, ValueError) as exc:
        console.print(f"[red]Extraction error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if workspace:
        console.print(f"Imported {len(workspace.observations)} HAR observations")
    if url:
        console.print(f"Website resources written to {output / 'website'}")
        if workspace:
            console.print(f"Unified workspace written to {output / 'workspace'}")
        report_path = output / "website" / "extraction.json"
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            console.print(
                f"Pages processed: {report.get('processed', 0)}; "
                f"skipped: {report.get('skipped', 0)}; "
                f"failed: {report.get('failed', 0)}; "
                f"downloaded: {report.get('downloaded_bytes', 0)} bytes"
            )
        if interactive:
            try:
                recipes = None
                if interactions is not None:
                    recipes_value = json.loads(interactions.read_text(encoding="utf-8"))
                    if not isinstance(recipes_value, list) or not all(isinstance(item, dict) for item in recipes_value):
                        raise ValueError("interactions file must contain a JSON list of objects")
                    recipes = recipes_value
                observations = __import__("asyncio").run(
                    discover_api_requests(url, storage_state=storage_state, interactions=recipes)
                )
            except (RuntimeError, OSError, ValueError) as exc:
                console.print(f"[red]Interactive discovery unavailable:[/red] {exc}")
                raise typer.Exit(code=1) from exc
            (output / "website" / "interactive.json").write_text(json.dumps(observations, indent=2), encoding="utf-8")
            console.print(f"Interactive JSON responses observed: {len(observations)}")


@app.command()
def wizard():
    """Interactively configure a website, HAR, or combined extraction."""
    mode = typer.prompt("Extraction mode (website, har, combined)", default="website").lower()
    if mode not in {"website", "har", "combined"}:
        raise typer.BadParameter("mode must be website, har, or combined")
    url = None if mode == "har" else typer.prompt("Website URL")
    har_values: list[Path] = []
    if mode in {"har", "combined"}:
        raw = typer.prompt("HAR files (comma-separated paths)")
        har_values = [Path(item.strip()) for item in raw.split(",") if item.strip()]
    scope = "page"
    resources = "html,css,js,images,json"
    if url:
        scope = typer.prompt("Scope (page or site)", default="page").lower()
        resources = typer.prompt("Resources (comma-separated)", default=resources)
    output = Path(typer.prompt("Output directory", default="extracted"))
    confirmed = typer.confirm(f"Extract {mode} data into {output}?", default=False)
    if not confirmed:
        console.print("Extraction cancelled.")
        return
    extract(
        url=url,
        har=har_values,
        output=output,
        yes=True,
        scope=scope,
        resources=resources,
        snapshot=typer.confirm("Generate an offline snapshot?", default=False) if url else False,
    )
