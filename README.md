# Local file automation with Ollama or GGUF

A Windows-friendly Python CLI and browser UI that use either local Ollama or a
GGUF model loaded directly through llama.cpp.
Python owns every path, backup, validation, retry, and write decision. The model receives
one system message and a bounded task/source message per call, without history, tools,
or an autonomous agent loop in the default workflow. Agent mode is a separate,
opt-in tool loop; it can execute code after you approve a request or choose
**Always allow this chat**.

The browser UI uses one continuous chat column with the composer below the
conversation, plus a Code/Text viewer and activity log in the same flow. Both
Ollama and local GGUF Agent replies stream as they are generated.

The request area is a conversation: after a model-backed workflow completes,
type a follow-up question and press Enter (Shift+Enter inserts a new line).
Follow-up answers stream into the chat and use the original result, generated
files, attached read-only references, public URLs, and recent conversation as
context. Ordinary follow-up questions are read-only. To run a literal command in
the same conversation, send `/run your-command`; the app shows its real exit code
and output. For model-selected actions, enable **Agent mode** in Settings: each
tool calls ask for approval until you choose **Always allow this chat**. A fenced `tool_code` block in model text
is not a tool call and is never executed. The selected model stays loaded for follow-ups
until a new task begins. The conversation survives a browser refresh while the
local app process remains open.

Choosing an Ollama model in **Workspace & model** preloads it immediately, so
the first prompt does not wait for the model to enter memory. Choosing another
Ollama model offloads the previous one first. **Offload loaded model** releases
the selected model without starting a new task; select it again to preload it.

Use **Add reference files** to select up to eight existing, read-only files as
model context. Text/code files must be UTF-8; PDF and DOCX text is extracted.
Each file is limited to 2 MB and reference text is bounded before sending it to
the model. Scanned or image-only PDFs cannot supply text. Attachments are never
output targets or overwritten by attaching them. To create or edit several
files, use `/file` lines or `@file` references in the request, or select a
project folder for mixed-type output (up to 20 files).

## Agent mode (browser UI)

Double-click [start.bat](./start.bat), select a workspace folder and a tool-capable
Ollama or GGUF model, then check **Agent mode**. Describe the task normally
(for example, "inspect this project, fix the failing test, and run it"). The model
may request `list_files`, `read_file`, `write_file`, `run_command`, and
`web_fetch` for public pages or GitHub repository README/metadata. The UI shows
the tool name and lets you expand its arguments before choosing **Allow once**,
**Always allow this chat**, or **Deny**. Always allow covers every later tool call
in this conversation, including files, commands, browser access, and enabled MCP
tools. The choice is saved with this conversation; new conversations ask again.
Files may be absolute or relative to the selected workspace; review absolute
paths before approval. File writes are limited to 64 KB and back up existing files.
Public URLs explicitly included in an Agent request are also read as task context,
so a small model can summarize a linked repository even if it does not make a
tool call. Local/private URLs are blocked. Follow-up questions can likewise
read public links included in the question or referenced from the original task.
Commands run from the selected workspace in PowerShell on Windows, with a 120-second
limit. Cancelling stops the agent between calls or during a command. The tool loop
stops after 20 calls; start a new request to continue. Command output and the
model's final answer are displayed in the activity panel.

The optional browser tool controls a headless Chromium page (`navigate`, `snapshot`,
`click`, `type`). Install its browser binary once after installing dependencies:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

Check **Enable MCP tools** in Agent mode to use the built-in, read-only workspace
text search plus the local Ollama MCP tools. No configuration file is required.
The local server also exposes `fetch_url`, `browser`, and `run_code` for public
URLs, headless Chromium, and Python/JavaScript/Java/npm execution. Install the
corresponding runtimes (`python`, `node`/`npm`, and `javac`/`java`) and Chromium
before using those tools. To add external MCP plugins, copy
[mcp_servers.example.json](./mcp_servers.example.json) to `mcp_servers.json` and
replace the example command with your MCP stdio server's command, arguments and
optional environment. Configured servers start alongside the built-in search;
MCP tool calls use the conversation's approval setting. MCP servers run as local programs,
so configure only ones you trust. Keep credentials in local environment values and
do not commit `mcp_servers.json` if it contains secrets. The browser and MCP options
are off by default. The original deterministic workflow remains the default.

## Directory structure

```text
ai-agent-mini/
├── config.py                 # Settings, Ollama health check and stateless calls
├── file_manager.py           # Path checks, locks, snapshots, backups, atomic recovery
├── prompter.py               # Line-based chunking, prompts, regex extraction, validation
├── main.py                   # CLI and fixed workflow
├── requirements.txt          # Ollama Python dependency
├── README.md
├── .gitignore
└── tests/
    └── test_workflow.py      # Offline safety regression tests
```

## Windows setup

Install Python **3.12 or later**. Ollama is optional. [start.bat](./start.bat)
creates the virtual environment and installs Python dependencies automatically.
For a manual setup, run these commands in PowerShell from this directory:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Virtual environment activation is unnecessary. For Ollama models, install
[Ollama for Windows](https://ollama.com/download/windows) and pull a model with
`ollama pull qwen2.5-coder:7b`. If Ollama is not already running, run
`ollama serve` in another terminal. The default endpoint is `http://localhost:11434`.
The health check lists installed models, preloads the chosen model with an empty prompt,
then checks loaded models through `ps`. It fails before any transformation call if the
service or model is unavailable. A backup is retained even when the health check fails.

If **Refresh** shows no Ollama models and `ollama list` is empty too, the Ollama
server is using a model directory without manifests. `OLLAMA_MODELS` must point
to the directory that directly contains `manifests` and `blobs`, not its parent.
Also check **Ollama desktop Settings > Model location**: on Windows this setting
may differ from the environment variable. Quit and restart Ollama after fixing
either location. The browser app never changes `OLLAMA_MODELS`; choosing a GGUF
only starts a separate llama.cpp runtime and does not move Ollama models.

### Use a GGUF without Ollama

In the browser UI, change **Model source** to **Local GGUF file**, select an
existing `.gguf` model file, and run a normal workflow or Agent mode. The app
checks the GGUF header, launches `llama-server` on loopback, and closes it when
the job ends. On Windows x64/arm64 it downloads and verifies the official CPU
runtime on first use and caches it in `.runtime/llama/`; subsequent runs do not
need the network. The model file itself is not downloaded. Select a model with
a suitable chat template for generation and tool calling. For a different
CPU/GPU build, set `LLAMA_SERVER_PATH` to your own `llama-server` executable.
On other platforms, install `llama-server` on `PATH` or set that environment
variable. Ollama does not need to be installed or running.

CLI example:

```powershell
.\.venv\Scripts\python.exe main.py '.\example.py' --instruction 'Improve the docstrings.' --gguf 'D:\Models\coder.gguf'
```

## Run

```powershell
.\.venv\Scripts\python.exe main.py 'C:\Projects\demo\example.py' --instruction 'Add a docstring to each function. Preserve behavior.'
```

To use another installed model and customize generation:

```powershell
ollama pull gemma3:4b
.\.venv\Scripts\python.exe main.py '.\example.py' --instruction 'Improve the function docstrings.' --model gemma3:4b --temperature 0.2 --max-tokens 4096 --chunk-chars 6000 --context-tokens 16384 --timeout 180
```

The example target must already exist. `--help` lists all options. Additional limits are
`--max-file-bytes` (default 1,000,000) and `--max-chunks` (default 200).
Use a local endpoint to keep source on your machine; setting `--endpoint` to a remote
server sends the selected source and instruction there. No source code is written to logs.

## Fixed workflow and recovery

1. Validate the file path, acquire an exclusive companion lock, and read a bounded UTF-8 snapshot.
2. Split input at complete line boundaries and write a timestamped, uniquely named `.bak`
   beside the source. Flush the backup before generation.
3. Check Ollama and load the selected model. Send each original chunk with the narrow instruction.
4. Require exactly one language-labelled fenced block. Extract it using a full-match regex,
   preserve its indentation, and reject prose, conflicting fences, empty output, and NULs.
5. Assemble all chunks. Parse Python with `ast.parse` or JSON with `json.loads`.
6. On a formatting, extraction, or static validation failure, regenerate from the original
   chunks with a stricter format reminder. There are **two retries** after the initial attempt,
   at most `3 × number_of_chunks` transformation calls. A failed attempt stops at its first
   failing chunk. Network, loading, truncation/token-limit, and disk errors fail immediately.
7. Stage output beside the source, flush it, verify the source still matches the snapshot,
   and atomically replace it. Preserve UTF-8 BOM, LF/CRLF style and final-newline convention.
8. Any exception or Ctrl+C triggers recovery. If no replacement occurred, the original is
   already intact. If our replacement is present, restore the verified backup atomically.
   External edits are preserved rather than overwritten during recovery. Backups are retained.

Exit codes: `0` success, `1` workflow failure, `2` CLI usage error, `130` cancellation.

For manual recovery, stop any active workflow, inspect the backup and current source, then
use the actual timestamped backup name printed in the log:

```powershell
Copy-Item -LiteralPath 'C:\Projects\demo\example.py.TIMESTAMP.ID.bak' -Destination 'C:\Projects\demo\example.py'
```

A forced termination can leave a `.workflow.lock` or temporary file. Remove those only
after verifying the process has stopped. Backups are not automatically pruned.

## Validation and practical limits

Run the offline tests without installing or starting Ollama:

```powershell
py -m unittest discover -s tests -v
```

Supported extensions are listed in [prompter.py](./prompter.py). Python and JSON have syntax
checks; other formats have fence/content validation only. Syntax validation cannot prove
correctness, preserve semantics, or detect every incomplete model edit. Review the diff
against the retained backup and run your project's tests before using generated changes.

Chunking is line-based, with no cross-chunk model memory. Use narrow, local changes; changes
requiring whole-file context should fit in one chunk. A line longer than the chunk budget
is rejected instead of truncated. Context settings include conservative prompt headroom,
but tokenization varies by model, so choose a model-supported context window and reduce
chunk size if needed. Output size and generation tokens are bounded.

Only UTF-8 text (optional BOM) with consistent LF or CRLF endings is supported. Empty files,
NUL-containing data, symlinks, junctions and hard-linked targets are rejected. Atomic
replacement preserves basic file mode, but does not promise preservation of custom Windows
ACLs, alternate data streams, creation timestamps or other specialized metadata. Use normal
local source files, with write permission to both the file and its directory.

The companion lock coordinates this utility's processes. The pre-replacement byte check
detects external changes, but is not an operating-system lock on arbitrary editors; a small
check-to-replace race remains. Close other writers while running. Atomic replacement and
flushing reduce partial-write risk; hard termination/power loss cannot run Python recovery,
so keep the durable backup. No real-model integration test is required by the offline suite.
