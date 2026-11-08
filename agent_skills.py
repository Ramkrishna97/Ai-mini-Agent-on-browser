"""Bounded, on-demand workspace instructions for the local agent."""

import json
from pathlib import Path
import re


SKILL_ROOT = Path(".agent") / "skills"
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
MAX_SKILLS = 30
MAX_SKILL_BYTES = 16_384


def _skill_file(workspace: Path, name: str, extra_root: str = "") -> Path:
    external = isinstance(name, str) and name.startswith("lmstudio:")
    if external:
        name = name.removeprefix("lmstudio:")
        if not extra_root:
            raise ValueError("No external skill folder selected.")
    if not isinstance(name, str) or not SKILL_NAME.fullmatch(name):
        raise ValueError("Invalid skill name.")
    root = Path(extra_root) if external else workspace.resolve()
    folder = root if external else root / SKILL_ROOT
    path = folder / name / "SKILL.md"
    # Reject links at every level; workspace instructions may not escape the workspace.
    for item in ((folder, path.parent, path) if external else (folder.parent, folder, path.parent, path)):
        if item.is_symlink() or (item.exists() and item.resolve() != item.absolute()):
            raise ValueError("Linked skill paths are not allowed.")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Skill path leaves the workspace.")
    return path


def _read(workspace: Path, name: str, extra_root: str = "") -> str:
    path = _skill_file(workspace, name, extra_root)
    if not path.is_file() or path.stat().st_size > MAX_SKILL_BYTES:
        raise ValueError("Skill missing or larger than 16 KB.")
    return path.read_text(encoding="utf-8-sig")


def list_skills(workspace: Path, extra_root: str = "") -> str:
    result = []
    for folder, prefix in ((workspace.resolve() / SKILL_ROOT, ""), (Path(extra_root) if extra_root else None, "lmstudio:")):
        if folder is None or not folder.is_dir() or folder.is_symlink():
            continue
        for entry in sorted(folder.iterdir()):
            if len(result) >= MAX_SKILLS:
                break
            if not entry.is_dir() or entry.is_symlink() or not SKILL_NAME.fullmatch(entry.name):
                continue
            try:
                content = _read(workspace, prefix + entry.name, extra_root)
            except (OSError, UnicodeError, ValueError):
                continue
            description = next((line.removeprefix("description:").strip().strip('"\'')
                                for line in content.splitlines()[:20] if line.startswith("description:")), "")
            result.append({"name": prefix + entry.name, "description": description[:300]})
    return json.dumps(result)


def load_skill(workspace: Path, name: str, extra_root: str = "") -> str:
    return _read(workspace, name, extra_root)
