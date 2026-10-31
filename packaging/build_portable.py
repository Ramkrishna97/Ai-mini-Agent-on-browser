"""Build-time helper; application sources are copied without modification."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


def main():
    root = Path(__file__).resolve().parent.parent
    output = Path(sys.argv[1]).resolve()
    if "--zip" in sys.argv[2:]:
        with zipfile.ZipFile(str(output) + ".zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in output.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(output.parent))
        return
    if os.name != "nt" or sys.version_info < (3, 12):
        raise RuntimeError("Build on Windows with Python 3.12 or newer.")
    import tkinter  # Fail early if the build Python lacks Tcl/Tk.

    base = Path(sys.base_prefix)
    runtime = output / "runtime"
    runtime.mkdir()
    # Copy a real Python installation, preserving subprocess and tkinter support.
    # Never copy the builder's global site-packages or unrelated application files.
    for name in ("Lib", "DLLs", "tcl"):
        source = base / name
        if source.is_dir():
            shutil.copytree(source, runtime / name,
                            ignore=shutil.ignore_patterns("site-packages", "__pycache__", "test", "tests", "idlelib"))
    for path in base.iterdir():
        if path.is_file() and (path.suffix.lower() in {".dll", ".exe"}
                               or path.name.lower().startswith("license")):
            shutil.copy2(path, runtime / path.name)
    if not (runtime / "python.exe").is_file():
        raise RuntimeError("A standard Windows Python installation is required to build.")
    packages = runtime / "Lib" / "site-packages"
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                    "--target", str(packages), "-r", str(root / "requirements.txt")], check=True)
    app = output / "app"
    app.mkdir()
    for path in root.glob("*.py"):
        shutil.copy2(path, app / path.name)
    for name in ("index.html", "ui.js", "ui.css", "requirements.txt", "mcp_servers.example.json",
                 "core_tool_manifest.json"):
        shutil.copy2(root / name, app / name)
    shutil.copy2(root / "stop.bat", output / "stop.bat")
    shutil.copy2(root / "packaging" / "README.md", output / "README.md")
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    subprocess.run([str(runtime / "python.exe"), "-c",
                    "import tkinter, ollama, mcp, playwright, pypdf, docx, ui_server; "
                    "from tkinter import filedialog; print('Bundled runtime imports passed.')"],
                   cwd=app, env=environment, check=True)
    subprocess.run([str(runtime / "python.exe"), str(app / "ui_server.py"), "--help"],
                   cwd=app, env=environment, check=True)


if __name__ == "__main__":
    main()
