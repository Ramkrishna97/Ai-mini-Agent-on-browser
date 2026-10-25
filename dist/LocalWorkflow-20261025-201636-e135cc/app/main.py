"""Deterministic single-file generation and CLI entry point."""

import argparse
import logging
from pathlib import Path

from config import GenerationError, ModelError, Settings, create_engine
from file_manager import (FileSafetyError, assert_missing, assert_unchanged, atomic_write,
                          create_backup, create_parent_dirs, read_snapshot, remove_empty_dirs,
                          rollback, validate_path, workflow_lock)
from prompter import (OutputError, build_messages, extract_code, language_for,
                      reject_prompt_echo, split_chunks, validate_code)
from project_templates import validate_file_contract

LOG = logging.getLogger("workflow")


def generate_content(source: str, instruction: str, language: str, settings: Settings,
                     engine, *, project_context: dict | None = None, reference_context: str = "") -> str:
    """Return validated text; this subroutine never writes files or executes code."""
    if not instruction.strip() or len(instruction) > 4000:
        raise ValueError("Instruction must contain 1–4000 characters.")
    creating = not source.strip()
    chunks = [""] if creating else split_chunks(source, settings.chunk_chars, settings.max_chunks)
    feedback = ""
    for attempt in range(3):
        try:
            replacements = []
            for index, chunk in enumerate(chunks, start=1):
                LOG.info("Attempt %d/3, chunk %d/%d", attempt + 1, index, len(chunks))
                messages = build_messages(
                    instruction, chunk, language, index, len(chunks), attempt,
                    creating=creating, project_context=project_context, feedback=feedback)
                if reference_context:
                    messages.insert(1, {"role": "user", "content": reference_context})
                response = engine.generate(messages)
                code = extract_code(response, language, settings.max_tokens * 32)
                reject_prompt_echo(code, language)
                if creating:
                    code = code.rstrip("\n") + "\n"
                elif chunk.endswith("\n"):
                    code += "\n"
                replacements.append(code)
            updated = "".join(replacements)
            validate_code(updated, language)
            if project_context:
                validate_file_contract(updated, project_context["current_file"]["path"])
            if len(updated.encode("utf-8")) > settings.max_file_bytes * 4:
                raise OutputError("Generated file exceeds the output size limit.")
            return updated
        except (OutputError, GenerationError) as exc:
            feedback = str(exc)
            LOG.warning("Generation rejected: %s", exc)
            if attempt == 2:
                raise
    raise AssertionError("Unreachable retry state")


def transform(file: str, instruction: str, settings: Settings, engine=None) -> Path | None:
    """Automatically create a missing file or back up and edit an existing file."""
    if not instruction.strip() or len(instruction) > 4000:
        raise ValueError("Instruction must contain 1–4000 characters.")
    path = validate_path(file, must_exist=False, allow_missing_parents=True)
    if not path.exists():
        return create_file(str(path), instruction, settings, engine)
    with workflow_lock(path):
        snapshot = read_snapshot(path, settings.max_file_bytes, allow_empty=True)
        backup = create_backup(snapshot)
        LOG.info("Detected existing file; backup created: %s", backup)
        candidate = None
        try:
            engine = engine if engine is not None else create_engine(settings)
            engine.health_check()
            updated = generate_content(snapshot.text, instruction, language_for(path), settings, engine)
            candidate = snapshot.encode(updated)
            atomic_write(path, candidate, snapshot.mode, lambda: assert_unchanged(snapshot))
            LOG.info("Success: updated %s; backup retained at %s", path, backup)
            return backup
        except BaseException:
            try:
                rollback(snapshot, backup, candidate)
                LOG.warning("Rollback complete; original file is intact.")
            except Exception as recovery_error:
                LOG.error("Automatic rollback unavailable: %s. Recover from %s", recovery_error, backup)
            raise


def create_file(file: str, instruction: str, settings: Settings, engine=None) -> None:
    """Validate new content before exclusive publication; existing files stay intact."""
    if not instruction.strip() or len(instruction) > 4000:
        raise ValueError("Instruction must contain 1–4000 characters.")
    path = validate_path(file, must_exist=False, allow_missing_parents=True)
    # Existing destinations are routed to transform by the orchestrator. This
    # low-level create primitive remains exclusive to protect concurrent writers.
    if path.exists():
        raise FileSafetyError("Destination already exists; use the automatic workflow to edit it.")
    made = create_parent_dirs(path)
    try:
        with workflow_lock(path):
            assert_missing(path)
            LOG.info("Detected new file: %s", path)
            engine = engine if engine is not None else create_engine(settings)
            engine.health_check()
            code = generate_content("", instruction, language_for(path), settings, engine)
            atomic_write(path, code.encode("utf-8"), 0o600, lambda: assert_missing(path), overwrite=False)
            LOG.info("Success: created %s", path)
    except BaseException:
        LOG.warning("Creation failed; no existing file was overwritten.")
        remove_empty_dirs(made)
        raise


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Deterministic local file/project generation with Ollama or GGUF.")
    cli.add_argument("destination", help="File or project folder; missing files and folders are created")
    cli.add_argument("--name", default="", help="Optional filename or project folder name inside the destination")
    cli.add_argument("--instruction", required=True, help="Task description (maximum 4000 characters)")
    cli.add_argument("--endpoint", default="http://localhost:11434")
    cli.add_argument("--model", help="Installed Ollama model; unnecessary for command/folder-only requests")
    cli.add_argument("--gguf", help="Local GGUF file to load directly using llama.cpp (no Ollama needed)")
    cli.add_argument("--temperature", type=float, default=0.2)
    cli.add_argument("--max-tokens", type=int, default=4096)
    cli.add_argument("--context-tokens", type=int, default=16384)
    cli.add_argument("--timeout", type=float, default=180)
    cli.add_argument("--chunk-chars", type=int, default=6000)
    cli.add_argument("--max-file-bytes", type=int, default=1_000_000)
    cli.add_argument("--max-chunks", type=int, default=200)
    return cli


def main() -> int:
    args = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.model and args.gguf:
            raise ValueError("Choose either --model or --gguf, not both.")
        if args.gguf:
            from gguf_engine import validate_gguf
            args.gguf = str(validate_gguf(args.gguf))
        settings = Settings(provider="gguf" if args.gguf else "ollama", endpoint=args.endpoint,
                            model_name=args.gguf or args.model,
                            temperature=args.temperature, max_tokens=args.max_tokens,
                            context_tokens=args.context_tokens, timeout=args.timeout,
                            chunk_chars=args.chunk_chars, max_file_bytes=args.max_file_bytes,
                            max_chunks=args.max_chunks) if args.model or args.gguf else None
        from workflow import run_workflow
        run_workflow(args.destination, args.instruction, settings, name=args.name)
        return 0
    except KeyboardInterrupt:
        LOG.error("Cancelled.")
        return 130
    except (OSError, ValueError, FileSafetyError, ModelError) as exc:
        LOG.error("Failed: %s", exc)
        return 1
    except Exception:
        LOG.exception("Unexpected failure.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
