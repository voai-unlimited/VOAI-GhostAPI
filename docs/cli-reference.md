# CLI reference

See `voai-ghostapi --help`. Existing commands are `import`, `inspect`, `validate`,
`generate`, and `serve`. Website and combined extraction use:

```text
voai-ghostapi extract --url URL --yes
voai-ghostapi extract --har capture.har --output extracted --yes
voai-ghostapi extract --url URL --har capture.har --snapshot --yes
```

Repeat `--har` for multiple captures. `--scope page|site|selected`, `--pages` (for selected scope), `--max-pages`, `--max-depth`, `--resources`, and the byte
limits bound crawling. `--resume` consumes a checkpoint after cancellation. Add
`--interactive --storage-state state.json --interactions interactions.json` to run
authorized read-only Playwright recipes. `preview SNAPSHOT` serves an offline copy
locally. Resource selection accepts `all` (Select All) and `clear` (Clear Selection);
unknown resource names are rejected. HAR files are prevalidated before confirmation.
`wizard` asks for the mode, resources, and confirmation interactively.
