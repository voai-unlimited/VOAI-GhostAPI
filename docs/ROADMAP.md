# Roadmap

## Completed in the extraction-engine-upgrade milestone

- Preserved HAR 1.2 import, sanitization, schema inference, workspace
  validation, deterministic FastAPI generation, and local serving.
- Added independent website-only, HAR-only, and combined extraction modes with
  explicit confirmation and non-interactive `--yes` automation.
- Added bounded same-origin crawling with sitemap discovery, robots handling,
  redirects, retries, cancellation checkpoints, resource limits, progress
  reporting, and final extraction reports.
- Added resource metadata, content-hash deduplication, authorized read-only
  Playwright observation, authenticated storage-state/manual-login support, and
  optional offline snapshot rewriting.
- Added regression fixtures for the extraction modes, resources, interactive
  discovery, security boundaries, cancellation, snapshots, mock generation, and
  CLI compatibility.

The completed milestone is an offline-first development tool. It is not a full
browser recording service, a crawler that bypasses access controls, or a claim
that every observed website can be reconstructed offline.

## 0.2.0

Stateful scenarios, response overrides, and Python/TypeScript SDK generation.
Add explicit fixture selection for multiple response variants while retaining
deterministic generation and workspace compatibility.

## 0.3.0

Optional endpoint explorer and mock configuration UI built on the existing CLI
backend. The UI should consume the extraction and inventory APIs rather than
duplicating crawling, sanitization, or schema logic.

## 0.4.0

Optional Ollama integration and offline AI-assisted documentation, enabled only
for sanitized local workspaces and never required for extraction or mock
generation.

## Future hardening

- Add broader fixture coverage for browser-specific rendering and difficult
  redirect, cache, and content-encoding combinations.
- Improve snapshot rewriting diagnostics while keeping unresolved references
  explicit instead of promising complete offline behavior.
- Evaluate a persistent crawl database only if checkpoint files become
  insufficient for the supported workload; retain the current simple format
  for local runs.
