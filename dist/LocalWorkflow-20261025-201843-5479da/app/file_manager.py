"""UTF-8 snapshots, exclusive workflow locks, durable backups and atomic writes."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid


class FileSafetyError(RuntimeError):
    """A file cannot safely be read, replaced, or restored."""


@dataclass(frozen=True)
class Snapshot:
    path: Path
    original: bytes
    text: str
    newline: str
    bom: bool
    mode: int

    def encode(self, text: str) -> bytes:
        text = text.replace("\r\n", "\n").replace("\n", self.newline)
        return (b"\xef\xbb\xbf" if self.bom else b"") + text.encode("utf-8")


def validate_location(value: str) -> Path:
    """Normalize a file or folder destination without requiring it to exist."""
    path = Path(value).expanduser().absolute()
    # Reject symlinks/junctions at every component to avoid surprising destinations.
    for component in (path, *path.parents):
        if component.is_symlink() or component.is_junction():
            raise FileSafetyError(f"Symlinks and junctions are not supported: {component}")
    return path.resolve(strict=False)


def validate_path(value: str, *, must_exist: bool = True, allow_missing_parents: bool = False) -> Path:
    path = validate_location(value)
    if not path.exists():
        if must_exist:
            raise FileSafetyError("The selected file no longer exists. Browse to select it again.")
        if not allow_missing_parents and not path.parent.is_dir():
            raise FileSafetyError("The output folder does not exist.")
        return path
    if not path.is_file():
        raise FileSafetyError("This destination is a folder; use the project workflow.")
    if path.stat().st_nlink > 1:
        raise FileSafetyError("Hard-linked files are not supported.")
    return path


@contextmanager
def workflow_lock(path: Path):
    """Coordinate this utility's processes; never remove somebody else's lock."""
    lock = path.with_name(path.name + ".workflow.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise FileSafetyError(f"Workflow lock exists: {lock}. Check for a running process before removing it.") from exc
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def read_snapshot(path: Path, max_bytes: int, *, allow_empty: bool = False) -> Snapshot:
    metadata = path.stat()
    if metadata.st_size > max_bytes:
        raise FileSafetyError(f"File exceeds {max_bytes} bytes.")
    with path.open("rb") as handle:
        original = handle.read(max_bytes + 1)
    if len(original) > max_bytes:
        raise FileSafetyError("File grew beyond the input size limit.")
    try:
        text = original.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileSafetyError("Only UTF-8 (optionally with BOM) files are supported.") from exc
    if (not text.strip() and not allow_empty) or "\x00" in text:
        raise FileSafetyError("Empty or binary files are not supported.")
    remainder = text.replace("\r\n", "")
    if "\r" in remainder or ("\r\n" in text and "\n" in remainder):
        raise FileSafetyError("Normalize mixed or CR-only line endings before proceeding.")
    return Snapshot(path, original, text.replace("\r\n", "\n"),
                    "\r\n" if "\r\n" in text else "\n",
                    original.startswith(b"\xef\xbb\xbf"), stat.S_IMODE(metadata.st_mode))


def assert_unchanged(snapshot: Snapshot, expected: bytes | None = None) -> None:
    validate_path(str(snapshot.path))
    expected = snapshot.original if expected is None else expected
    with snapshot.path.open("rb") as handle:
        current = handle.read(len(expected) + 1)
    if current != expected:
        raise FileSafetyError("Target changed externally; refusing to overwrite it. Backup retained.")


def create_backup(snapshot: Snapshot) -> Path:
    assert_unchanged(snapshot)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = snapshot.path.with_name(f"{snapshot.path.name}.{timestamp}.{uuid.uuid4().hex[:8]}.bak")
    with backup.open("xb") as handle:
        handle.write(snapshot.original)
        handle.flush()
        os.fsync(handle.fileno())
    return backup


def atomic_write(path: Path, data: bytes, mode: int, before_replace=None, *, overwrite=True) -> None:
    """Stage on the same volume, fsync, then atomically replace the target."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if before_replace is not None:
            before_replace()
        if overwrite:
            os.replace(temporary, path)
        elif os.name == "nt":
            # Windows rename fails if a destination appears concurrently.
            os.rename(temporary, path)
        else:
            # POSIX rename overwrites; linking publishes exclusively instead.
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def assert_missing(path: Path) -> None:
    validate_path(str(path), must_exist=False)
    if path.exists():
        raise FileSafetyError("The destination appeared during generation; refusing to overwrite it.")


def safe_relative_path(value: str) -> Path:
    """Validate Windows-safe relative output names, even when checked on another OS."""
    if not isinstance(value, str) or not value or len(value) > 180:
        raise FileSafetyError("Output names must contain 1–180 characters.")
    parts = value.replace("\\", "/").split("/")
    for part in parts:
        if (not part or part in {".", ".."} or part.endswith((".", " "))
                or re.search(r'[<>:"|?*\x00-\x1f]', part)
                or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                                                    *[f"COM{i}" for i in range(1, 10)],
                                                    *[f"LPT{i}" for i in range(1, 10)]}):
            raise FileSafetyError(f"Unsafe or invalid output name: {value!r}")
    return Path(*parts)


def create_parent_dirs(path: Path) -> list[Path]:
    """Create only missing directories and remember exactly which ones we own."""
    validate_location(str(path))
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    if not parent.is_dir():
        raise FileSafetyError("An output parent is a file, not a directory.")
    made = []
    try:
        for directory in reversed(missing):
            directory.mkdir()
            made.append(directory)
    except BaseException:
        remove_empty_dirs(made)
        raise
    return made


def remove_empty_dirs(directories: list[Path]) -> None:
    """Never recursively delete; preserve any external files created meanwhile."""
    for directory in reversed(directories):
        try:
            directory.rmdir()
        except OSError:
            pass


def rollback(snapshot: Snapshot, backup: Path, candidate: bytes | None) -> None:
    """Restore our replacement only; a rejected generation leaves the original intact."""
    if backup.read_bytes() != snapshot.original:
        raise FileSafetyError(f"Backup integrity check failed: {backup}")
    try:
        assert_unchanged(snapshot)
        return  # Original bytes are already present; restoration is a safe no-op.
    except FileSafetyError:
        if candidate is None:
            raise
    assert_unchanged(snapshot, candidate)
    atomic_write(snapshot.path, snapshot.original, snapshot.mode,
                 lambda: assert_unchanged(snapshot, candidate))
