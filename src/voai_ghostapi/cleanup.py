from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

WORKSPACE_NAMES = frozenset({"workspace", "workspace-new", "workspace-fixed"})
GENERATED_NAMES = frozenset({"generated", "generated-new", "generated-fixed"})


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    kind: str
    reason: str = ""


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_workspace(path: Path) -> bool:
    marker = path / "workspace.json"
    if not marker.is_file() or marker.is_symlink():
        return False
    try:
        import json

        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("version") == "1" and isinstance(
        data.get("observations"), list
    )


def _is_generated(path: Path) -> bool:
    app = path / "app.py"
    readme = path / "README.md"
    if not app.is_file() or app.is_symlink() or not readme.is_file() or readme.is_symlink():
        return False
    try:
        text = app.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return "VOAI-GhostAPI mock" in text and "FastAPI" in text


def _unexpected(path: Path, kind: str) -> bool:
    allowed = {"workspace.json"} if kind == "workspace" else {"app.py", "README.md"}
    return any(item.name not in allowed for item in path.iterdir())


def find_candidates(root: Path) -> tuple[list[CleanupCandidate], list[CleanupCandidate]]:
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("cleanup root must be a real directory")
    candidates: list[CleanupCandidate] = []
    skipped: list[CleanupCandidate] = []
    for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if path.is_symlink() or not _within(path.absolute(), root):
            if path.name in WORKSPACE_NAMES or path.name in GENERATED_NAMES:
                skipped.append(CleanupCandidate(path, "unknown", "symlink or unsafe path"))
            continue
        kind = "workspace" if path.name in WORKSPACE_NAMES else "generated" if path.name in GENERATED_NAMES else ""
        if not kind:
            continue
        recognized = _is_workspace(path) if kind == "workspace" else _is_generated(path)
        if not recognized:
            skipped.append(CleanupCandidate(path, kind, "missing recognizable VOAI markers"))
        elif _unexpected(path, kind):
            skipped.append(CleanupCandidate(path, kind, "contains unexpected user files"))
        else:
            candidates.append(CleanupCandidate(path, kind))
    return candidates, skipped


def clean_artifacts(root: Path, *, dry_run: bool = False) -> tuple[list[CleanupCandidate], list[CleanupCandidate], list[str]]:
    candidates, skipped = find_candidates(root)
    errors: list[str] = []
    if not dry_run:
        for candidate in candidates:
            try:
                shutil.rmtree(candidate.path)
            except (OSError, PermissionError) as exc:
                errors.append(f"{candidate.path}: {exc}")
    return candidates, skipped, errors

