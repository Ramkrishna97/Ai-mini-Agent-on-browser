"""Prepare a bundled IDE backend and start it without opening a browser."""

import os
from pathlib import Path
import subprocess
import sys


def main():
    home = Path(__file__).resolve().parent
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required to start AI Agent Mini.")
    python = home / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(home / ".venv")], check=True)
    check = subprocess.run([str(python), "-c", "import ui_server"], cwd=home,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode:
        print("Preparing AI Agent Mini Python dependencies...", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(home / "requirements.txt")],
                       cwd=home, check=True)
    subprocess.run([str(python), str(home / "ui_server.py"), "--no-browser", *sys.argv[1:]],
                   cwd=home, check=True)


if __name__ == "__main__":
    main()
