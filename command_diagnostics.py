"""Extract a small, actionable summary from failed local command output."""

import re

MAX_ERRORS = 6
MAX_LINE = 300

PYTEST_FAILURE = re.compile(r"^FAILED\s+(\S+)(?:\s+-\s+(.+))?$")
JAVA_ERROR = re.compile(r"^(.*?\.java):(\d+):\s*error:\s*(.+)$", re.I)
TS_ERROR = re.compile(r"^(\S+\.(?:[cm]?[jt]sx?))\((\d+),\d+\):\s*error\s+\w+:\s*(.+)$", re.I)
EXCEPTION = re.compile(r"^([\w.]+(?:Error|Exception|Failure)|AssertionError):\s*(.*)$")
GENERIC_ERROR = re.compile(r"^(?:\[ERROR\]\s*|npm\s+ERR!\s*|error:\s*)(.+)$", re.I)


def parse_command_result(exit_code: int, stdout: str, stderr: str) -> dict:
    """Return bounded diagnostics that an agent can use for its next edit."""
    if exit_code == 0:
        return {"status": "success", "summary": "Command completed successfully.", "errors": []}

    errors = []
    seen = set()
    lines = [line.strip() for text in (stderr, stdout) for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not line or line in seen or line == "[earlier output omitted]":
            continue
        match = PYTEST_FAILURE.match(line)
        if match:
            entry = {"kind": "failed_test", "message": (match.group(2) or "Test failed")[:MAX_LINE],
                     "test": match.group(1)[:MAX_LINE]}
        elif match := JAVA_ERROR.match(line):
            entry = {"kind": "compiler_error", "message": match.group(3)[:MAX_LINE],
                     "file": match.group(1)[-MAX_LINE:], "line": int(match.group(2))}
        elif match := TS_ERROR.match(line):
            entry = {"kind": "compiler_error", "message": match.group(3)[:MAX_LINE],
                     "file": match.group(1)[-MAX_LINE:], "line": int(match.group(2))}
        elif match := EXCEPTION.match(line):
            entry = {"kind": "exception", "message": line[:MAX_LINE]}
        elif match := GENERIC_ERROR.match(line):
            entry = {"kind": "error", "message": match.group(1)[:MAX_LINE]}
        elif line == "* What went wrong:" and index + 1 < len(lines):
            entry = {"kind": "build_error", "message": lines[index + 1][:MAX_LINE]}
        else:
            continue
        seen.add(line)
        errors.append(entry)
        if len(errors) == MAX_ERRORS:
            break

    if not errors:
        tail = next((line for line in reversed(lines) if line and line != "[earlier output omitted]"), "No error output captured.")
        errors = [{"kind": "command_failure", "message": tail[:MAX_LINE]}]
    first = errors[0]
    summary = f"Command exited with code {exit_code}: {first['message']}"
    if first.get("test"):
        summary += f" ({first['test']})"
    return {"status": "failed", "summary": summary[:500], "errors": errors}
