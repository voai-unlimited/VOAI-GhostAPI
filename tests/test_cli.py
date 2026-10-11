import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from voai_ghostapi.cli import ExtractionDashboard, app

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
    assert runner.invoke(app, ["extract", "--help"]).exit_code == 0
    assert runner.invoke(app, ["wizard", "--help"]).exit_code == 0
    assert runner.invoke(app, ["preview", "--help"]).exit_code == 0


def test_extraction_dashboard_reports_counters_without_percentage():
    dashboard = ExtractionDashboard("website")
    dashboard.update(
        {
            "stage": "page",
            "current": "https://example.test/",
            "pages_discovered": 4,
            "pages_completed": 1,
            "pages_skipped": 1,
            "pages_failed": 0,
            "resources_downloaded": 2,
            "unique_apis_discovered": 3,
            "downloaded_bytes": 1024,
            "elapsed_seconds": 1.5,
        }
    )
    rendered = dashboard.render()
    assert dashboard.state["pages_discovered"] == 4
    assert dashboard.state["pages_completed"] == 1
    assert rendered.title == "VOAI-GhostAPI extraction"


def test_preview_rejects_non_snapshot_directory(tmp_path):
    result = runner.invoke(app, ["preview", str(tmp_path)])
    assert result.exit_code != 0


def test_preview_serves_a_valid_snapshot(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "snapshot.json").write_text('{"files": 1}', encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "voai_ghostapi.cli.subprocess.run",
        lambda args, cwd, check: calls.append((args, cwd, check)),
    )
    result = runner.invoke(app, ["preview", str(snapshot), "--port", "8123"])
    assert result.exit_code == 0
    assert calls and calls[0][0][3] == "8123" and calls[0][1] == snapshot


def test_extract_requires_an_input_source(tmp_path):
    result = runner.invoke(app, ["extract", "--output", str(tmp_path / "out"), "--yes"])
    assert result.exit_code != 0
    assert result.exception is not None


def test_extract_preflights_har_before_confirmation(tmp_path):
    bad = tmp_path / "bad.har"
    bad.write_text("not-json", encoding="utf-8")
    result = runner.invoke(
        app,
        ["extract", "--har", str(bad), "--output", str(tmp_path / "out")],
        input="n\n",
    )
    assert result.exit_code != 0
    assert "invalid HAR" in result.output
    assert "Start extraction" not in result.output


def test_extract_resource_selection_rejects_unknown_value(tmp_path):
    capture = tmp_path / "capture.har"
    make_har(capture)
    result = runner.invoke(
        app,
        [
            "extract",
            "--har",
            str(capture),
            "--resources",
            "unknown",
            "--output",
            str(tmp_path / "out"),
            "--yes",
        ],
    )
    assert result.exit_code != 0
    assert "unsupported resource" in result.output


def test_extract_writes_final_report_for_har_mode(tmp_path):
    capture = tmp_path / "capture.har"
    make_har(capture)
    output = tmp_path / "extracted"
    result = runner.invoke(app, ["extract", "--har", str(capture), "--output", str(output), "--yes"])
    assert result.exit_code == 0
    report = json.loads((output / "extraction-report.json").read_text(encoding="utf-8"))
    assert report["mode"] == "har"
    assert report["discovered_apis"] == ["GET /items/{param}"]


def test_large_extract_requires_explicit_threshold_confirmation(tmp_path):
    result = runner.invoke(
        app,
        ["extract", "--url", "https://example.test", "--scope", "site", "--max-pages", "50", "--yes", "--output", str(tmp_path / "out")],
    )
    assert result.exit_code != 0
    # Rich may wrap the option name at a terminal-width-dependent boundary.
    # Normalize whitespace before checking the safety diagnostic so this test
    # remains stable across the Windows, Linux, and macOS runners.
    diagnostic = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    diagnostic = re.sub(r"\s+", "", diagnostic)
    assert "confirm-large" in diagnostic


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
    assert "unexpected" in " ".join(result.stdout.split())


def test_clean_confirmation_refusal_and_approval(tmp_path):
    workspace = tmp_path / "workspace-new"
    workspace.mkdir()
    (workspace / "workspace.json").write_text('{"version":"1","observations":[]}')
    refused = runner.invoke(app, ["clean", "--all", "--root", str(tmp_path)], input="n\n")
    assert refused.exit_code == 0 and workspace.exists()
    approved = runner.invoke(app, ["clean", "--all", "--root", str(tmp_path)], input="y\n")
    assert approved.exit_code == 0 and not workspace.exists()


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


def test_clean_rejects_symlinked_parent(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    marker = real / "workspace-new"
    marker.mkdir()
    (marker / "workspace.json").write_text('{"version":"1","observations":[]}')
    parent = tmp_path / "parent-link"
    try:
        parent.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    result = runner.invoke(app, ["clean", "--all", "--yes", "--root", str(parent)])
    assert result.exit_code == 1 and marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-specific")
def test_clean_rejects_windows_junction(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "workspace-new"
    marker.mkdir()
    (marker / "workspace.json").write_text('{"version":"1","observations":[]}')
    junction = tmp_path / "junction"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("junction creation unavailable")
    result = runner.invoke(app, ["clean", "--all", "--yes", "--root", str(junction)])
    assert result.exit_code == 1
    assert marker.exists()
