"""Execute explicit user commands with live output and bounded cancellation."""

import codecs
import logging
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

LOG = logging.getLogger("workflow")


def run_command(command: str, cwd: Path, *, timeout: float = 120, cancel_check=None) -> dict:
    if os.name == "nt":
        executable = shutil.which("powershell.exe")
        if not executable:
            raise RuntimeError("Windows PowerShell is unavailable.")
        argv = [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); " + command]
    else:
        argv = ["/bin/sh", "-c", command]
    LOG.info("Running explicit user command in %s: %s", cwd, command)
    environment = os.environ.copy()
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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

    def read_output():
        try:
            while not stop_reader.is_set():
                chunk = process.stdout.read1(4096)
                if not chunk:
                    break
                enqueue(chunk)
        finally:
            enqueue(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    last_heartbeat = time.monotonic()
    tail, total, eof = b"", 0, False
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while not eof or process.poll() is None:
            if cancel_check:
                cancel_check()
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Explicit command exceeded its {timeout:g}-second time limit.")
            try:
                chunk = chunks.get(timeout=0.1)
                if chunk is None:
                    eof = True
                else:
                    total += len(chunk)
                    if total > 4_000_000:
                        raise RuntimeError("Explicit command exceeded the 4 MB output limit.")
                    tail = (tail + chunk)[-65536:]
                    text = decoder.decode(chunk).rstrip()
                    if text:
                        LOG.info("Command output: %s", text)
            except queue.Empty:
                pass
            if time.monotonic() - last_heartbeat >= 5:
                LOG.info("Command still running (%ds elapsed)", int(timeout - (deadline - time.monotonic())))
                last_heartbeat = time.monotonic()
        process.wait()
    except BaseException:
        try:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                else:
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        raise
    finally:
        stop_reader.set()
        reader.join(timeout=2)
        process.stdout.close()
    text = tail.decode("utf-8", errors="replace")
    if total > 65536:
        text = "[earlier output omitted]\n" + text
    LOG.info("Command exit code: %d", process.returncode)
    return {"command": command, "exit_code": process.returncode, "output": text}
