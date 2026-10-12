# Development

Use Python 3.11+, install `.[dev]`, and run pytest plus Ruff. Browser integration
tests additionally need `pip install -e '.[browser]'` and a Playwright Chromium
runtime. Keep test inputs synthetic and never commit storage-state files, HAR
captures containing credentials, or generated output directories.

The complete local checks are:

```text
python -m ruff check src tests
python -m mypy src
python -m pytest --cov=voai_ghostapi --cov-report=term-missing
python -m build
```

## Extraction Studio workspace

Run the local Studio from the repository with:

```powershell
voai-ghostapi studio --open --root .
```

Studio stores project and job metadata in `.studio.sqlite3` beneath the
selected root. Large extraction outputs remain on disk under the project
directory. The database stores configuration snapshots, lifecycle state,
progress counters, checkpoint references, and result-manifest paths; it does
not store HAR contents, storage-state credentials, or downloaded binaries.

The Studio workflow is: choose Website, HAR, or Combined mode; configure the
source and scope; discover and explicitly select pages when needed; choose
resources and limits; review the security boundary; then start a monitored
job. Discovery is read-only and bounded. Extraction is performed by the
existing Python crawler and selected-page scope is enforced by the backend.

The monitor uses ordered job events and supports cancellation, checkpoint
resume, retry of failed jobs, logs, and partial-result inspection. A job that
is interrupted or cancelled is never reported as completed.

Projects expose API observations, resource manifests and safe previews,
offline snapshots, run comparison, incremental validator-aware resource
requests, and generated local FastAPI mocks. Mocks bind to `127.0.0.1`, use
controlled process arguments, and can be stopped only when managed by Studio.

There is no Node.js runtime requirement. The static frontend is packaged with
the Python wheel. Frontend source changes can be smoke-tested by launching
Studio and opening the local dashboard; the repository currently has no
`package.json` or separate frontend build pipeline.

## Verification and packaging

Use a Python 3.12 environment with a compatible `httpx` 0.x release:

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q --basetemp="$PWD\.pytest-local"
python -m ruff check src tests
python -m mypy src
python -m coverage run -m pytest -q
python -m coverage report
python -m build
```

If pytest's system temporary directory is locked on Windows, keep
`--basetemp` inside the repository. If packaging fails with a missing
Hatchling backend, install the development dependencies before rerunning the
build.
