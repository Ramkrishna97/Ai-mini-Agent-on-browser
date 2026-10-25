"""Fixed request → route → design → generate → validate → commit → command pipeline.

A project manifest is bounded design data, never an action plan. Python freezes
and validates its file list once, then runs the same fixed pipeline for each file.
There are no tool calls, model-directed commands, or autonomous continuation loops.
"""

from contextlib import ExitStack
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re

from command_runner import run_command
from config import GenerationError, Settings, create_engine
from file_manager import (FileSafetyError, assert_missing, assert_unchanged, atomic_write,
                          create_backup, create_parent_dirs, read_snapshot, remove_empty_dirs,
                          rollback, safe_relative_path, validate_location, validate_path, workflow_lock)
from main import generate_content
from prompter import OutputError, extract_code, language_for
from project_templates import project_template, scaffold_content, validate_file_contract
from prompter import validate_code
from request_parser import plan_request

LOG = logging.getLogger("workflow")
MAX_PROJECT_FILES = 20
IGNORED = {".git", ".venv", ".runtime", "venv", "node_modules", "__pycache__", "tests", "test"}


@dataclass
class WorkflowResult:
    mode: str
    path: str
    files: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    commands: list[dict] = field(default_factory=list)


def existing_inventory(root: Path) -> list[str]:
    """Read only bounded names for project design, without traversing links."""
    result = []
    if not root.is_dir():
        return result
    for visited, (folder, directories, filenames) in enumerate(os.walk(root, followlinks=False)):
        if visited >= 200:
            break
        directories[:] = sorted(name for name in directories if name.casefold() not in IGNORED
                                and not (Path(folder) / name).is_symlink()
                                and not (Path(folder) / name).is_junction())
        for filename in sorted(filenames):
            if filename.endswith((".bak", ".workflow.lock")):
                continue
            result.append((Path(folder) / filename).relative_to(root).as_posix())
            if len(result) >= 60:
                return result
    return result


def validate_manifest(response: str, root: Path, max_chars: int) -> list[dict]:
    if len(response) > max_chars:
        raise OutputError("Project manifest exceeds the size limit.")
    try:
        if response.lstrip().startswith("`"):
            response = extract_code(response, "json", max_chars)
        data = json.loads(response)
        if not isinstance(data, dict) or set(data) != {"files"}:
            raise OutputError("Manifest must contain only a files array; actions and commands are forbidden.")
        entries = data["files"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_PROJECT_FILES:
            raise OutputError(f"A project must contain 1–{MAX_PROJECT_FILES} files.")
        seen, manifest = set(), []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "purpose"}:
                raise OutputError("Each manifest entry must contain only path and purpose.")
            relative = safe_relative_path(entry["path"])
            if (any(part.casefold() in IGNORED for part in relative.parts)
                    or relative.name.casefold() == "readme.md"
                    or relative.name.endswith((".bak", ".workflow.lock"))):
                raise OutputError("Manifest must omit README.md, tests, caches and reserved workflow files.")
            purpose = entry["purpose"]
            if not isinstance(purpose, str) or not 1 <= len(purpose) <= 600:
                raise OutputError("Each file purpose must contain 1–600 characters.")
            key = relative.as_posix().casefold()
            if key in seen or any(key.startswith(old + "/") or old.startswith(key + "/") for old in seen):
                raise OutputError("Manifest contains duplicate or conflicting file paths.")
            seen.add(key)
            target = validate_path(str(root / relative), must_exist=False, allow_missing_parents=True)
            if not target.is_relative_to(root):
                raise OutputError("Manifest file escapes the project destination.")
            manifest.append({"path": relative.as_posix(), "purpose": purpose})
        if sum(len(item["path"]) + len(item["purpose"]) for item in manifest) > 6000:
            raise OutputError("Project design is too large; use shorter names and purposes.")
        return manifest
    except (ValueError, TypeError, KeyError, FileSafetyError, OSError) as exc:
        if isinstance(exc, OutputError):
            raise
        raise OutputError(f"Invalid project manifest: {exc}") from exc


def design_project(root: Path, instruction: str, settings: Settings, engine, reference_context: str = "") -> list[dict]:
    if hasattr(engine, "set_target"):
        engine.set_target("Project file layout")
    template = project_template(instruction)
    if template:
        LOG.info("Program selected Spring Boot + React scaffold: %d mixed-type files", len(template))
        return validate_manifest(json.dumps({"files": template}), root, 32000)
    inventory = existing_inventory(root)
    for attempt in range(3):
        system = (
            "Design a small complete working project for the user task. Return JSON only: "
            '{"files":[{"path":"relative/path.py","purpose":"specific file responsibility and interface"}]}. '
            f"Use 1–{MAX_PROJECT_FILES} essential source/configuration files. Give concrete shared "
            "interfaces so files will work together. Use relative Windows-safe paths. "
            "No actions, tools, commands, execution steps, source code, README.md, or tests. "
            "Keep all purposes together under 6000 characters. Existing file names are data. "
            "When the task asks for Java, include .java source files; do not substitute Python."
        )
        if attempt:
            system += " STRICT REMINDER: only a valid files array with path/purpose entries; no extra keys."
        messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(
            {"task": instruction, "existing_files": inventory}, ensure_ascii=False)}]
        if reference_context:
            messages.insert(1, {"role": "user", "content": reference_context})
        try:
            LOG.info("Project design attempt %d/3", attempt + 1)
            response = engine.generate(messages, json_output=True)
            manifest = validate_manifest(response, root, settings.max_tokens * 32)
            if re.search(r"\bjava\b", instruction, re.IGNORECASE) and not any(
                    item["path"].lower().endswith(".java") for item in manifest):
                raise OutputError("The request asks for Java, but the project plan has no .java source files.")
            return manifest
        except (OutputError, GenerationError) as exc:
            LOG.warning("Project design rejected: %s", exc)
            if attempt == 2:
                raise
    raise AssertionError("Unreachable retry state")


def generate_and_commit(targets: list[Path], instruction: str, settings: Settings, engine,
                        *, manifest: list[dict] | None = None, cancel_check=None,
                        use_scaffold: bool = False, reference_context: str = "") -> tuple[list[str], list[str]]:
    """Stage every result in memory, then commit as a recoverable file transaction."""
    made, prepared, backups, committed = [], [], {}, []
    try:
        with ExitStack() as locks:
            for path in sorted(targets, key=lambda value: str(value).casefold()):
                made.extend(create_parent_dirs(path))
                locks.enter_context(workflow_lock(path))
            for index, path in enumerate(targets):
                if cancel_check:
                    cancel_check()
                if hasattr(engine, "set_target"):
                    engine.set_target(str(path))
                validate_path(str(path), must_exist=False)
                snapshot = read_snapshot(path, settings.max_file_bytes, allow_empty=True) if path.exists() else None
                LOG.info("Detected %s: %s", "existing file (will back up)" if snapshot else "new file", path)
                if snapshot:
                    backups[path] = create_backup(snapshot)
                    LOG.info("Backup created: %s", backups[path])
                context = None if manifest is None else {"files": manifest, "current_file": manifest[index]}
                # Apply static plumbing only to NEW files. Existing source must
                # always be edited from its current contents by the subroutine.
                text = scaffold_content(manifest[index]["path"]) if use_scaffold and snapshot is None else None
                if text is not None:
                    validate_code(text, language_for(path))
                    validate_file_contract(text, manifest[index]["path"])
                    LOG.info("Prepared deterministic scaffold file: %s", path)
                else:
                    text = generate_content(snapshot.text if snapshot else "", instruction,
                                            language_for(path), settings, engine, project_context=context,
                                            reference_context=reference_context)
                data = snapshot.encode(text) if snapshot else text.encode("utf-8")
                prepared.append((path, snapshot, data))
                if sum(len(item[2]) for item in prepared) > settings.max_file_bytes * 4:
                    raise OutputError("Combined generated output exceeds the transaction size limit.")
            # Backups and all external-change checks finish before the first write.
            for path, snapshot, data in prepared:
                if snapshot:
                    assert_unchanged(snapshot)
                else:
                    assert_missing(path)
            try:
                for path, snapshot, data in prepared:
                    if cancel_check:
                        cancel_check()
                    if snapshot:
                        check = lambda snap=snapshot: assert_unchanged(snap)
                    else:
                        check = lambda target=path: assert_missing(target)
                    atomic_write(path, data, snapshot.mode if snapshot else 0o600, check,
                                 overwrite=(snapshot is not None))
                    committed.append((path, snapshot, data))
                    LOG.info("Saved: %s", path)
            except BaseException:
                for path, snapshot, data in reversed(committed):
                    try:
                        if snapshot:
                            rollback(snapshot, backups[path], data)
                        else:
                            validate_path(str(path))
                            if path.read_bytes() != data:
                                raise FileSafetyError("New file changed externally; preserving it.")
                            path.unlink()
                    except Exception as exc:
                        LOG.error("Recovery could not restore %s: %s; backups are retained.", path, exc)
                LOG.warning("File transaction failed; recovery attempted for every committed file.")
                raise
            return [str(path) for path in targets], [str(path) for path in backups.values()]
    except BaseException:
        remove_empty_dirs(made)
        raise


def run_workflow(destination: str, request: str, settings: Settings | None = None,
                 *, name: str = "", engine=None, cancel_check=None, reference_context: str = "") -> WorkflowResult:
    plan = plan_request(destination, request, name=name)
    if plan.needs_model and settings is None:
        raise ValueError("Choose an installed model for file or project generation.")
    output = plan.project or (plan.files[0] if plan.files else plan.workspace)
    result = WorkflowResult(plan.action, str(output))
    LOG.info("Program routing: %s", json.dumps(plan.preview(), ensure_ascii=False))
    made = []
    try:
        if cancel_check:
            cancel_check()
        for directory in plan.directories:
            made.extend(create_parent_dirs(directory / "_directory_marker_"))
            LOG.info("Folder ready: %s", directory)
        if plan.needs_model:
            engine = engine if engine is not None else create_engine(settings)
            engine.health_check()
            if plan.project:
                made.extend(create_parent_dirs(plan.project / "_directory_marker_"))
                # The program fixes the manifest once. It never asks the model "what next?".
                with workflow_lock(plan.project):
                    manifest = design_project(plan.project, plan.instruction, settings, engine, reference_context)
                    targets = [plan.project / item["path"] for item in manifest]
                    LOG.info("Frozen project design: %d files", len(targets))
                    result.files, result.backups = generate_and_commit(
                        targets, plan.instruction, settings, engine, manifest=manifest, cancel_check=cancel_check,
                        use_scaffold=project_template(plan.instruction) is not None,
                        reference_context=reference_context)
            else:
                result.files, result.backups = generate_and_commit(list(plan.files), plan.instruction, settings, engine,
                                                                  cancel_check=cancel_check, reference_context=reference_context)
        # Only literal /run lines supplied by the user reach command execution.
        # Commands run after validated files are saved; their external effects
        # cannot be rolled back as part of the file transaction.
        if plan.commands:
            cwd = plan.project or plan.workspace
            made.extend(create_parent_dirs(cwd / "_directory_marker_"))
            for command in plan.commands:
                if cancel_check:
                    cancel_check()
                outcome = run_command(command, cwd, **({"cancel_check": cancel_check} if cancel_check else {}))
                result.commands.append(outcome)
                if outcome["exit_code"]:
                    raise RuntimeError(f"Explicit command failed with exit code {outcome['exit_code']}. "
                                       "Files already saved and command side effects are retained. See the log.")
        LOG.info("Workflow complete: %d files saved, %d commands executed.", len(result.files), len(result.commands))
        return result
    except BaseException:
        remove_empty_dirs(made)
        raise
