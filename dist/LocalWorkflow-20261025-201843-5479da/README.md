# Portable Windows build

## Build after making changes

Double-click `build-exe.bat` in the project root. The builder needs Windows,
Python 3.12+ with pip and Tcl/Tk, and internet access to install dependencies.
It prefers the project's existing `.venv`, then `py`, then `python`.
Windows PowerShell's built-in .NET compiler creates the small executable launcher.
No application source files are modified. Every build gets a new directory under
`dist/` and a matching ZIP; earlier builds are retained.

The builder bundles a private Python installation, installs `requirements.txt`
into it, copies the current root Python modules and browser assets, and validates
imports and the server command-line entry point before producing the ZIP.
Dependencies follow the version ranges in `requirements.txt`, so builds made on
different days may resolve different dependency versions.

## Run on another Windows computer

1. Extract the entire ZIP into a writable local folder.
2. Double-click `LocalWorkflow.exe`. The browser opens automatically.
3. Keep the console open. Press Ctrl+C to stop, or use `stop.bat` for the default port.

Share the whole ZIP, not the executable alone. Keep `app/` and `runtime/` beside
the executable. No Python, pip, Java, npm, or Docker installation is needed to
launch the interface. Use a Windows computer with the same architecture as the
build machine and an OS supported by the bundled Python version. The launcher
uses Windows' .NET Framework. This is a portable folder, not an installer or a
single-file executable. The launcher is unsigned.

## Models and optional tools

- Select **Local GGUF file** to use the app without Ollama. Supply a compatible
  model separately. The existing app downloads its verified llama.cpp CPU runtime
  on first GGUF use, so that first use needs internet. For offline use, provide
  a compatible runtime and set `LLAMA_SERVER_PATH` before launch.
- Ollama mode still needs an installed or reachable Ollama service and model.
- Java compilation, Node/npm commands, Docker commands, and external MCP servers
  still need their corresponding tools. They are not included in this package.
- Optional browser automation needs Chromium. Install it into the normal user
  browser cache using `runtime\python.exe -m playwright install chromium` from
  the extracted folder. That download is separate from the ZIP.
- Add external MCP configuration as `app/mcp_servers.json` on the destination
  machine. The build intentionally excludes local configurations, credentials,
  model files, runtime caches, generated projects, and conversation data.

The bundled Python remains a real interpreter, so the existing file-dialog,
Python execution, and built-in MCP subprocesses can run unchanged.
