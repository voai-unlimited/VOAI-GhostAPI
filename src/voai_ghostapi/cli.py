from pathlib import Path

import typer
import uvicorn
from rich.console import Console

from . import __version__
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

