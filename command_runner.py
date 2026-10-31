"""Execute explicit user commands with live output and bounded cancellation."""

import codecs
import logging
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

from command_diagnostics import parse_command_result

LOG = logging.getLogger("workflow")
COMMAND_TIMEOUT_SECONDS = 60
MAX_OUTPUT_BYTES = 4_000_000
OUTPUT_TAIL_BYTES = 65_536


def uses_cmd_chain(command: str) -> bool:
    """Recognize CMD-style chaining outside quoted strings on Windows."""
    quote = None
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char in ("`", "^"):
            escaped = True
            continue
        if char in ("'", '"'):
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        elif quote is None and command[index:index + 2] in ("&&", "||"):
            return True
    return False


def run_command(command: str, cwd: Path, *, timeout: float = COMMAND_TIMEOUT_SECONDS, cancel_check=None) -> dict:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Command timeout must be a finite positive number.")
    timeout = min(timeout, COMMAND_TIMEOUT_SECONDS)
    if os.name == "nt":
        shell_name = "cmd" if uses_cmd_chain(command) else "powershell"
        executable = shutil.which("cmd.exe" if shell_name == "cmd" else "powershell.exe")
        if not executable:
            raise RuntimeError(f"Windows {shell_name} is unavailable.")
        argv = ([executable, "/d", "/c", command] if shell_name == "cmd" else
                [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                 "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); " + command])
    else:
        shell_name = "sh"
        argv = ["/bin/sh", "-c", command]
    LOG.info("Running explicit user command with %s in %s: %s", shell_name, cwd, command)
    environment = os.environ.copy()
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                               start_new_session=(os.name != "nt"))
    chunks = queue.Queue(maxsize=256)
    stop_reader = threading.Event()

    def enqueue(value):
        while not stop_reader.is_set():
            try:
                chunks.put(value, timeout=0.1)
                return
            except queue.Full:
                continue

    def read_output(stream_name, stream):
        try:
            while not stop_reader.is_set():
                chunk = stream.read1(4096)
                if not chunk:
                    break
                enqueue((stream_name, chunk))
        finally:
            enqueue((stream_name, None))

    readers = [threading.Thread(target=read_output, args=(name, stream), daemon=True)
               for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    last_heartbeat = time.monotonic()
    tails = {"output": b"", "stdout": b"", "stderr": b""}
    totals = {"output": 0, "stdout": 0, "stderr": 0}
    eof = set()
    decoders = {name: codecs.getincrementaldecoder("utf-8")(errors="replace")
                for name in ("stdout", "stderr")}
    try:
        while len(eof) < 2 or process.poll() is None:
            if cancel_check:
                cancel_check()
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Explicit command exceeded its {timeout:g}-second time limit.")
            try:
                stream_name, chunk = chunks.get(timeout=0.1)
                if chunk is None:
                    eof.add(stream_name)
                else:
                    totals["output"] += len(chunk)
                    totals[stream_name] += len(chunk)
                    if totals["output"] > MAX_OUTPUT_BYTES:
                        raise RuntimeError("Explicit command exceeded the 4 MB output limit.")
                    tails["output"] = (tails["output"] + chunk)[-OUTPUT_TAIL_BYTES:]
                    tails[stream_name] = (tails[stream_name] + chunk)[-OUTPUT_TAIL_BYTES:]
                    text = decoders[stream_name].decode(chunk).rstrip()
                    if text:
                        LOG.info("Command output (%s): %s", stream_name, text)
            except queue.Empty:
                pass
            if time.monotonic() - last_heartbeat >= 5:
                LOG.info("Command still running (%ds elapsed)", int(timeout - (deadline - time.monotonic())))
                last_heartbeat = time.monotonic()
        process.wait()
    except BaseException:
        if process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
                else:
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
            except (OSError, subprocess.TimeoutExpired):
                LOG.warning("Could not terminate the complete command process tree", exc_info=True)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        raise
    finally:
        stop_reader.set()
        for reader in readers:
            reader.join(timeout=2)
        process.stdout.close()
        process.stderr.close()
    result = {}
    for name in tails:
        value = tails[name].decode("utf-8", errors="replace")
        if totals[name] > OUTPUT_TAIL_BYTES:
            value = "[earlier output omitted]\n" + value
        result[name] = value
    LOG.info("Command exit code: %d", process.returncode)
    diagnostics = parse_command_result(process.returncode, result["stdout"], result["stderr"])
    return {"command": command, "shell": shell_name, "exit_code": process.returncode,
            "diagnostics": diagnostics, **result}
