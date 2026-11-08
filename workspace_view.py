"""Bounded, read-only workspace browsing for the local web interface."""

from pathlib import Path

from file_manager import FileSafetyError, validate_location

MAX_ENTRIES = 200
MAX_SCAN = 1000
MAX_PREVIEW_BYTES = 128_000
HIDDEN_NAMES = {".git", ".venv", ".runtime", "node_modules", "__pycache__"}


def workspace_target(root_value: str, relative: str = "") -> tuple[Path, Path]:
    if not isinstance(root_value, str) or len(root_value) > 2000 or not Path(root_value).is_absolute():
        raise ValueError("Choose an absolute workspace folder.")
    if not isinstance(relative, str) or len(relative) > 2000:
        raise ValueError("Invalid workspace path.")
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts:
        raise ValueError("Workspace path must stay inside the selected folder.")
    root = validate_location(root_value)
    if root.is_file():
        root = root.parent
    if not root.is_dir():
        raise ValueError("Workspace folder does not exist.")
    target = validate_location(str(root / part))
    if not target.is_relative_to(root):
        raise FileSafetyError("Workspace path leaves the selected folder.")
    return root, target


def list_workspace(root_value: str, relative: str = "") -> dict:
    root, directory = workspace_target(root_value, relative)
    if not directory.is_dir():
        raise ValueError("Workspace folder does not exist.")
    entries = []
    truncated = False
    for scanned, path in enumerate(directory.iterdir()):
        if scanned >= MAX_SCAN:
            truncated = True
            break
        if path.name in HIDDEN_NAMES or path.is_symlink() or path.is_junction():
            continue
        try:
            folder = path.is_dir()
            if not folder and not path.is_file():
                continue
        except OSError:
            continue
        entries.append({"name": path.name, "path": path.relative_to(root).as_posix(),
                        "type": "directory" if folder else "file"})
    entries.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold()))
    if len(entries) > MAX_ENTRIES:
        truncated = True
    return {"root": str(root), "relative": directory.relative_to(root).as_posix()
            if directory != root else "", "entries": entries[:MAX_ENTRIES], "truncated": truncated}


def read_workspace_file(root_value: str, relative: str) -> dict:
    root, path = workspace_target(root_value, relative)
    if path == root or not path.is_file():
        raise ValueError("Choose a file in this workspace.")
    with path.open("rb") as handle:
        data = handle.read(MAX_PREVIEW_BYTES + 1)
    if len(data) > MAX_PREVIEW_BYTES:
        raise ValueError("File is too large to preview (128 KB limit).")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Only UTF-8 text files can be previewed.") from exc
    if "\x00" in text:
        raise ValueError("Binary files cannot be previewed.")
    return {"path": str(path), "text": text}
