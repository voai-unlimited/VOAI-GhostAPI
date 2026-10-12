# CLI reference

Run `voai-ghostapi --help` for the installed command list. The original offline
HAR workflow is unchanged:

```text
voai-ghostapi import capture.har --output workspace
voai-ghostapi inspect workspace
voai-ghostapi validate workspace
voai-ghostapi generate workspace --output generated
voai-ghostapi serve generated --port 8000
```

The extraction command supports each input mode independently:

```text
# Website only
voai-ghostapi extract --url https://example.com --scope page --yes

# HAR only; repeat --har for multiple captures
voai-ghostapi extract --har capture.har --output extracted --yes

# Website plus HAR observations
voai-ghostapi extract --url https://example.com --har capture.har \
  --scope site --max-pages 20 --max-depth 2 --output extracted --yes
```

Without `--yes`, extraction displays a preflight summary and requires explicit
confirmation. `wizard` asks for the mode, URL/HAR inputs, scope, resources,
output, and confirmation interactively. `--scope page` processes one page;
`--scope site` enables bounded same-origin crawling; `--scope selected` requires
`--pages /path-one,/path-two` and does not follow unrelated links.

Use `--resources` with `html`, `css`, `js`, `images`, `svg`, `fonts`, `json`,
`manifests`, `icons`, `video`, `audio`, `documents`, `all`, or `clear`. Bound
website work with `--max-pages`, `--max-depth`, `--concurrency`, `--crawl-delay`,
`--max-resources`, `--max-resource-mb`, `--max-total-mb`, and `--max-seconds`.
`--resume` continues from a retained checkpoint. Large automated runs require
`--confirm-large` after the warning threshold is reached.

Optional read-only browser observation requires the browser extra and a local
Playwright runtime:

```text
python -m pip install ".[browser]"
playwright install chromium
voai-ghostapi extract --url https://example.com --interactive \
  --storage-state state.json --interactions interactions.json --yes
```

Authorized storage-state files and visible `--manual-login` sessions are
supported. Passwords, cookies, authorization headers, and browser storage are
not persisted. Interactive discovery records observed requests only and
rejects state-changing or destructive recipes.

`--snapshot` writes an offline copy under `snapshot/`; `preview SNAPSHOT` serves
it locally:

```text
voai-ghostapi extract --url https://example.com --snapshot --yes
voai-ghostapi preview extracted/snapshot --port 8001
```

`clean --all` previews recognized generated artifacts and asks before deletion.
Use `--dry-run`, `--yes`, or `--root PATH` to control the cleanup operation.
HAR captures, virtual environments, source code, Git metadata, symlinks, and
unrelated directories are protected.

The optional local Extraction Studio is launched with:

```text
voai-ghostapi studio --open
```

It binds to `127.0.0.1` by default. The Studio uses the same extraction
pipeline as the CLI, requires explicit confirmation in its wizard, streams
actual job events, and exposes sanitized project, API, resource, and snapshot
views. Node.js is not required to run an installed Studio build.
