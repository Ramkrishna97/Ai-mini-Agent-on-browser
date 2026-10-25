"""Bounded completion suggestions from reserved words and the local filesystem."""

from pathlib import Path

from file_manager import validate_location
from request_parser import RESERVED_WORDS


def suggestions(destination: str, kind: str, prefix: str) -> list[dict]:
    if not isinstance(prefix, str) or len(prefix) > 500:
        raise ValueError("Completion prefix is too long.")
    if kind == "command":
        marker = prefix[:1] if prefix[:1] in {"/", "!", "#"} else "/"
        query = prefix.lstrip("/!#").casefold()
        return [{"label": marker + item["name"], "insert": marker + item["name"] + " ",
                 "detail": item["detail"], "kind": "command"}
                for item in RESERVED_WORDS if item["name"].startswith(query)]
    if kind != "file":
        raise ValueError("Unknown completion type.")
    root = validate_location(destination)
    if root.is_file() or (not root.exists() and root.suffix):
        root = root.parent
    normalized = prefix.replace("\\", "/")
    parent, separator, query = normalized.rpartition("/")
    if not separator:
        parent, query = "", normalized
    directory = Path(parent + "/") if parent else root
    if not directory.is_absolute():
        directory = root / directory
    directory = validate_location(str(directory))
    if not directory.is_dir():
        return []
    entries = []
    # Scan a bounded number of names; never read file contents for completion.
    for visited, entry in enumerate(directory.iterdir()):
        if visited >= 2000:
            break
        if (entry.name.casefold() in {".git", ".venv", ".runtime", "node_modules", "__pycache__"}
                or entry.name.endswith((".bak", ".workflow.lock"))
                or not entry.name.casefold().startswith(query.casefold())
                or entry.is_symlink() or entry.is_junction()):
            continue
        folder = entry.is_dir()
        value = (parent + "/" if separator else "") + entry.name + ("/" if folder else "")
        entries.append({"label": entry.name + ("/" if folder else ""), "insert": value,
                        "detail": "Folder" if folder else "File", "kind": "folder" if folder else "file"})
    return sorted(entries, key=lambda item: (item["kind"] != "folder", item["label"].casefold()))[:30]
