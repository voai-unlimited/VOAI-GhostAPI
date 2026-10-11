# CLI reference

See `voai-ghostapi --help`. Existing commands are `import`, `inspect`, `validate`,
`generate`, and `serve`. Website and combined extraction use:

```text
voai-ghostapi extract --url URL --yes
voai-ghostapi extract --har capture.har --output extracted --yes
voai-ghostapi extract --url URL --har capture.har --snapshot --yes
```

`--scope page|site`, `--max-pages`, `--max-depth`, `--resources`, and the byte
limits bound crawling. `--resume` consumes a checkpoint after cancellation. Add
`--interactive --storage-state state.json --interactions interactions.json` to run
authorized read-only Playwright recipes. `preview SNAPSHOT` serves an offline copy
locally. `wizard` asks for the mode and confirmation interactively.
