"""Deterministic request syntax. No model is consulted for routing or commands.

@"relative or absolute file.py" selects files. /file, /project, /mkdir and
/run (also !run, #mkdir, etc.) are explicit line-based directives.
"""

from dataclasses import dataclass
from pathlib import Path
import re

from file_manager import FileSafetyError, safe_relative_path, validate_location, validate_path


REFERENCES = re.compile(r'''(?<![\w@])@(?:"([^"]+)"|'([^']+)'|`([^`]+)`|([^\s<>"'`,;!?()]+))''')
DIRECTIVE = re.compile(r"^\s*[/!#](file|project|mkdir|run)\b\s*(.*)$", re.IGNORECASE)
RESERVED_WORDS = [
    {"name": "file", "detail": "Create or edit a named file", "example": "/file src/app.py"},
    {"name": "project", "detail": "Generate mixed file types from your description", "example": "/project my-app"},
    {"name": "mkdir", "detail": "Create a folder without a model", "example": "/mkdir output/reports"},
    {"name": "run", "detail": "Execute your literal PowerShell command", "example": "/run Get-ChildItem"},
]
BARE_COMMAND = re.compile(
    r"^(?:wsl(?:\.exe)?\s+--|ollama\s+(?:list|ps|run|pull|show|serve|stop)\b|"
    r"(?:python(?:\.exe)?|py)\s+(?:-[mc]\b|[^\s]+\.py\b)|"
    r"(?:npm|pnpm|yarn)\s+(?:install|run|start|test|build|dev)\b|"
    r"git\s+(?:status|diff|log|branch|clone|fetch|pull|push|add|commit|switch|checkout)\b|"
    r"mvn\s+(?:clean|compile|package|test|spring-boot:run)\b|"
    r"dotnet\s+(?:build|run|restore|test|new)\b)", re.IGNORECASE)


@dataclass(frozen=True)
class RequestPlan:
    workspace: Path
    instruction: str
    files: tuple[Path, ...] = ()
    project: Path | None = None
    directories: tuple[Path, ...] = ()
    commands: tuple[str, ...] = ()

    @property
    def needs_model(self) -> bool:
        return bool(self.files or self.project)

    @property
    def action(self) -> str:
        if self.project:
            return "project"
        if self.files:
            if len(self.files) > 1:
                return "files"
            return "edit" if self.files[0].exists() else "create"
        return "commands" if self.commands else "folders"

    def preview(self) -> dict:
        return {"action": self.action, "workspace": str(self.workspace),
                "files": [{"path": str(path), "action": "edit" if path.exists() else "create"}
                          for path in self.files],
                "project": str(self.project) if self.project else "",
                "directories": [str(path) for path in self.directories],
                "commands": list(self.commands), "needs_model": self.needs_model}


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'`":
        value = value[1:-1]
    return value


def resolve_reference(value: str, workspace: Path) -> Path:
    value = unquote(value)
    if not value:
        raise ValueError("A directive or @ reference is missing its path.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        # Explicit user references may name a parent folder; model manifests
        # still use strict safe_relative_path and cannot escape their root.
        for part in value.replace("\\", "/").split("/"):
            if part not in {".", ".."}:
                safe_relative_path(part)
        path = workspace / path
    return validate_location(str(path))


def plan_request(destination: str, request: str, *, name: str = "") -> RequestPlan:
    """Filesystem facts and explicit syntax determine all operations."""
    if not isinstance(request, str) or not request.strip() or len(request) > 4000:
        raise ValueError("Enter a request of 1–4000 characters.")
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError("Choose a workspace folder or file.")
    selected = validate_location(destination)
    workspace = selected.parent if selected.is_file() or (not selected.exists() and selected.suffix) else selected
    if name:
        if selected.is_file():
            raise ValueError("An output name can only be used with a folder destination.")
        selected = validate_location(str(selected / safe_relative_path(name)))
    files, projects, directories, commands, prose = [], [], [], [], []
    has_directives = False
    for line in request.splitlines():
        match = DIRECTIVE.fullmatch(line)
        if not match:
            if BARE_COMMAND.match(line.strip()):
                commands.append(line.strip())
                has_directives = True
                continue
            if re.fullmatch(r"\s*[/!#][A-Za-z]*\s*", line):
                raise ValueError("Choose a reserved word: /file, /project, /mkdir or /run.")
            prose.append(line)
            continue
        has_directives = True
        command, argument = match[1].lower(), match[2].strip()
        if command == "run":
            if not argument or len(argument) > 2000:
                raise ValueError("/run requires an explicit command of 1–2000 characters.")
            commands.append(argument)
        elif command == "file":
            files.append(resolve_reference(argument, workspace))
        elif command == "mkdir":
            directories.append(resolve_reference(argument, workspace))
        else:
            projects.append(resolve_reference(argument, workspace) if argument else
                            (selected if not selected.is_file() else workspace))
    text = "\n".join(prose)
    references = list(REFERENCES.finditer(text))
    for match in references:
        value = next(group for group in match.groups() if group is not None)
        if match[4] is not None:
            value = value.rstrip(".:")  # Sentence punctuation cannot end a Windows filename.
        files.append(resolve_reference(value, workspace))
    # Annotation paths are metadata, not natural-language instructions to the model.
    instruction = REFERENCES.sub("", text).strip()
    if len(projects) > 1 or (projects and files):
        raise ValueError("Use one /project destination or @file / /file targets in a request, not both.")
    if not files and not projects and not has_directives:
        if selected.is_file() or (not selected.exists() and (selected.suffix or selected.name.lower() in {"dockerfile", ".gitignore"})):
            files.append(selected)
        else:
            projects.append(selected)
    unique = {}
    for path in files:
        validated = validate_path(str(path), must_exist=False, allow_missing_parents=True)
        unique.setdefault(str(validated).casefold(), validated)
    if len(unique) > 20 or len(directories) > 20 or len(commands) > 5:
        raise ValueError("Request limit: 20 files, 20 folders, and 5 explicit commands.")
    for directory in directories + projects:
        if directory.exists() and not directory.is_dir():
            raise FileSafetyError(f"Folder destination is a file: {directory}")
    if (unique or projects) and not instruction:
        raise ValueError("Describe what the selected files or project should contain, in addition to the path directive.")
    return RequestPlan(workspace, instruction, tuple(unique.values()), projects[0] if projects else None,
                       tuple(directories), tuple(commands))
