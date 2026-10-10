# Security

Please report vulnerabilities privately to the project maintainers. Do not include real credentials or captures in issues. Imported data is sanitized and never sent over the network.

Generated output directories must be empty and may not be symlinks or traverse symlinked parents. The check is performed before path resolution; a process racing the check with a filesystem rename could still affect the destination, so run generation in a trusted workspace with normal filesystem permissions.

Workspace validation checks the version, methods, routes, schemas, and detectable sensitive URL exposure. Automated redaction is conservative and cannot guarantee removal of every proprietary secret; review sanitized workspaces before sharing them.

