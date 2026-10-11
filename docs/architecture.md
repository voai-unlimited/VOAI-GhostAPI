# Architecture

VOAI-GhostAPI keeps the original HAR-to-FastAPI pipeline and adds a bounded website
pipeline around the same observation model. `core.py` owns Pydantic observations,
sanitization, schema inference, workspace persistence, and deterministic FastAPI
generation. `extract.py` validates origins, follows only same-origin links, applies
page/depth/byte limits, reads nested sitemap indexes, uses bounded breadth-first
request batches with crawl-delay rate limiting, records resource metadata, writes
resumable checkpoints, and merges optional HAR observations into one workspace.
`interactive.py` is an optional
Playwright adapter for authorized, read-only recipes; it records observed JSON
responses without replaying requests.

Website files remain under `website/`, API observations under `workspace/`, and an
optional rewritten offline copy under `snapshot/`. Redirects are validated before a
follow, private destinations are blocked, and resource paths use content hashes.
Snapshots cannot reproduce server-side behavior, authentication, or third-party
integrations that were not downloaded.
