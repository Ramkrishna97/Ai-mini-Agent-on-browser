"""Bounded project scaffolding, patches, and owned development processes."""

import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid

from file_manager import (assert_missing, assert_unchanged, atomic_write,
                          create_backup, read_snapshot, validate_location, validate_path)

MAX_FILES = 100
MAX_FILE_BYTES = 65_536
MAX_TOTAL_BYTES = 1_000_000
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def create_project_structure(workspace: Path, base_path: str, file_tree: dict[str, str]) -> dict:
    workspace = workspace.resolve()
    base = validate_location(str(workspace / base_path))
    if not base.is_relative_to(workspace) or not isinstance(file_tree, dict) or not 1 <= len(file_tree) <= MAX_FILES:
        raise ValueError("Choose a base path in the workspace and 1-100 files.")
    targets = []
    total = 0
    for relative, content in file_tree.items():
        if not isinstance(relative, str) or not relative or not isinstance(content, str):
            raise ValueError("Each fileTree entry needs a relative file path and text content.")
        path = validate_location(str(base / relative))
        if not path.is_relative_to(base) or path == base or path.exists():
            raise ValueError(f"File must be new and inside the project: {relative}")
        encoded = content.encode("utf-8")
        total += len(encoded)
        if len(encoded) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Project exceeds the per-file or total size limit.")
        targets.append((path, encoded))
    if len({path for path, _ in targets}) != len(targets):
        raise ValueError("Duplicate project file destinations.")
    created = []
    try:
        for path, content in targets:
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = 0o700 if path.name in {"gradlew", "mvnw"} or path.suffix == ".sh" else 0o600
            atomic_write(path, content, mode, lambda p=path: assert_missing(p))
            created.append(path)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return {"base_path": str(base), "files": [str(path) for path in created]}


def apply_file_patch(workspace: Path, file_path: str, target_diff: str) -> dict:
    path = validate_path(str(validate_location(str(workspace / file_path))))
    if not path.is_relative_to(workspace.resolve()):
        raise ValueError("Patch target must be inside the workspace.")
    if not isinstance(target_diff, str) or not 0 < len(target_diff) <= 100_000:
        raise ValueError("targetDiff must be a unified diff of at most 100 KB.")
    snapshot = read_snapshot(path, 1_000_000, allow_empty=True)
    original = snapshot.text.splitlines(keepends=True)
    diff = target_diff.replace("\r\n", "\n").splitlines(keepends=True)
    result = []
    cursor = 0
    index = 0
    count = 0
    while index < len(diff):
        header = HUNK.match(diff[index])
        if not header:
            if count == 0 and (diff[index].startswith("--- ") or diff[index].startswith("+++ ")):
                index += 1
                continue
            raise ValueError("Expected a unified diff hunk header.")
        old_start, old_count, _, new_count = header.groups()
        old_count = int(old_count or 1)
        new_count = int(new_count or 1)
        start = int(old_start) - (0 if old_count == 0 else 1)
        if start < cursor or start > len(original):
            raise ValueError("Patch hunks overlap or lie outside the file.")
        result.extend(original[cursor:start])
        cursor = start
        index += 1
        consumed = produced = 0
        while index < len(diff) and not diff[index].startswith("@@ "):
            line = diff[index]
            if line.startswith("\\ No newline at end of file"):
                index += 1
                continue
            if not line or line[0] not in " +-":
                raise ValueError("Invalid unified diff line.")
            text = line[1:]
            if index + 1 < len(diff) and diff[index + 1].startswith("\\ No newline at end of file"):
                text = text.removesuffix("\n")
            if line[0] in " -":
                if cursor >= len(original) or original[cursor] != text:
                    raise ValueError("Patch context does not match the file.")
                cursor += 1
                consumed += 1
            if line[0] in " +":
                result.append(text)
                produced += 1
            index += 1
        if consumed != old_count or produced != new_count:
            raise ValueError("Patch hunk line counts are incorrect.")
        count += 1
    if not count:
        raise ValueError("Patch has no hunks.")
    result.extend(original[cursor:])
    updated = "".join(result)
    if updated == snapshot.text or len(updated.encode("utf-8")) > 1_000_000:
        raise ValueError("Patch makes no change or exceeds 1 MB.")
    backup = create_backup(snapshot)
    atomic_write(path, snapshot.encode(updated), snapshot.mode, lambda: assert_unchanged(snapshot))
    return {"file_path": str(path), "hunks": count, "backup": str(backup)}


def wait_for_port(port: int, timeout_ms: int, cancel_check=None) -> dict:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("port must be 1-65535.")
    if not isinstance(timeout_ms, int) or not 0 <= timeout_ms <= 120_000:
        raise ValueError("timeoutMs must be 0-120000.")
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if cancel_check:
            cancel_check()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return {"port": port, "ready": True, "url": f"http://127.0.0.1:{port}"}
        except OSError:
            if time.monotonic() >= deadline:
                return {"port": port, "ready": False}
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))


class DevServerManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.processes = {}
        self.logs = {}

    def start(self, command: str, port: int, timeout_ms: int = 2_000) -> dict:
        if not isinstance(command, str) or not 0 < len(command) <= 2000:
            raise ValueError("command must contain 1-2000 characters.")
        if any(item["port"] == port for item in self.processes.values()):
            raise ValueError("A managed server already owns this port.")
        if wait_for_port(port, 0)["ready"]:
            raise ValueError("Port is already in use.")
        if os.name == "nt":
            shell = shutil.which("cmd.exe")
            argv = [shell, "/d", "/c", command] if shell else None
        else:
            argv = ["/bin/sh", "-c", command]
        if not argv:
            raise RuntimeError("Command shell unavailable.")
        process = subprocess.Popen(argv, cwd=self.workspace, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                   start_new_session=os.name != "nt")
        process_id = uuid.uuid4().hex
        self.processes[process_id] = {"process": process, "port": port, "command": command}
        self.logs[process_id] = bytearray()

        def drain():
            while chunk := process.stdout.read1(4096):
                log = self.logs.get(process_id)
                if log is not None:
                    log.extend(chunk)
                    if len(log) > 65_536:
                        del log[:-65_536]
            process.stdout.close()

        threading.Thread(target=drain, daemon=True).start()
        ready = wait_for_port(port, timeout_ms)
        if not ready["ready"] and process.poll() is not None:
            output = self.logs[process_id].decode("utf-8", errors="replace")[-4000:]
            self.stop(process_id)
            raise RuntimeError(f"Server exited before port {port} became ready: {output}")
        return {"process_id": process_id, "pid": process.pid, "port": port,
                "ready": ready["ready"], "url": f"http://127.0.0.1:{port}",
                "output": self.logs[process_id].decode("utf-8", errors="replace")[-4000:]}

    def stop(self, process_id: str) -> dict:
        item = self.processes.pop(process_id, None)
        if item is None:
            raise ValueError("Unknown managed process ID.")
        process = item["process"]
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
            else:
                import signal
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.logs.pop(process_id, None)
        return {"process_id": process_id, "stopped": True, "exit_code": process.returncode}

    def close(self):
        for process_id in list(self.processes):
            self.stop(process_id)
