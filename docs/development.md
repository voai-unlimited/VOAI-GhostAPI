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
