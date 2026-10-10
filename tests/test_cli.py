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

