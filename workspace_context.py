"""Cheap, bounded project overview for IDE-driven agent turns."""

import json
from pathlib import Path
import threading
import time


SKIP = {".git", ".venv", "venv", "node_modules", "dist", "build", ".gradle", ".idea", ".runtime"}
MANIFESTS = {"pyproject.toml", "requirements.txt", "package.json", "pom.xml", "build.gradle",
             "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "Cargo.toml", "go.mod"}
_CACHE = {}
_LOCK = threading.Lock()


def workspace_overview(workspace: Path) -> str:
    root = workspace.resolve()
    if not root.is_dir():
        raise ValueError("Workspace root must be a directory.")
    stamp = root.stat().st_mtime_ns
    with _LOCK:
        cached = _CACHE.get(str(root))
        if cached and cached[0] == stamp and time.monotonic() - cached[1] < 60:
            return cached[2]
    entries = []
    manifests = []
    queue = [(root, 0)]
    while queue and len(entries) < 180:
        folder, depth = queue.pop(0)
        try:
            children = sorted(folder.iterdir(), key=lambda item: (item.is_file(), item.name.casefold()))
        except OSError:
            continue
        for child in children:
            if len(entries) >= 180:
                break
            if child.is_symlink() or child.name in SKIP or child.name.endswith((".class", ".pyc", ".bak")):
                continue
            relative = child.relative_to(root).as_posix()
            if child.is_dir():
                entries.append(relative + "/")
                if depth < 2:
                    queue.append((child, depth + 1))
            elif depth <= 1:
                entries.append(relative)
                if child.name in MANIFESTS:
                    manifests.append(relative)
    result = json.dumps({"root": str(root), "entries": entries, "manifests": manifests,
                         "truncated": len(entries) >= 180}, ensure_ascii=False)
    with _LOCK:
        _CACHE[str(root)] = (stamp, time.monotonic(), result)
    return result
