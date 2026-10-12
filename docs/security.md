# Security model

Resource limits, local-only serving, path-controlled output directories, and secret redaction are enforced in the core workflow.

Studio adds defense in depth around that workflow: output and artifact paths
must remain beneath the approved root and symlinks are rejected; HAR uploads
are extension-, encoding-, and size-validated; extracted snapshots are served
with a restrictive content security policy; downloaded JavaScript is never
executed by Studio; and storage-state files are accepted only as authorized
inputs and are never exposed through API responses.

Website discovery and extraction retain URL validation, same-origin redirect
checks, DNS/private-network protections, robots handling, bounded page/depth
limits, and explicit selected-page scope. Incremental reuse is based on
origin validators such as ETag or Last-Modified, never URL equality alone.

Generated mocks are started with an argument array using the configured Python
interpreter, bind to `127.0.0.1`, track their process IDs, detect unavailable
ports, capture logs locally, and stop only processes created by Studio. Studio
does not expose arbitrary local files, enable uncontrolled CORS, execute
captured scripts, or send telemetry.
