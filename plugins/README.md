# IDE adapters

These are installable editor layers over the existing Python backend. The web
launcher remains `../start.bat` and still opens the browser app.

| IDE | Source | Installable package |
| --- | --- | --- |
| VS Code | [vscode/](./vscode/) | [VSIX](./vscode/ai-agent-mini-0.3.0.vsix) |
| IntelliJ IDEA | [intellij/](./intellij/) | [ZIP](./intellij/build/distributions/ai-agent-mini-intellij-0.3.0.zip) |

The packages use `http://127.0.0.1:8765` by default (configurable in VS Code
settings or with `AI_AGENT_MINI_PORT` for IntelliJ). Open a local workspace/project; the
plugin bundles the backend and its **Start** button prepares and launches it
without asking for an app folder. First start requires Python 3.12+ and may
download Python dependencies. Choosing or loading an Ollama model starts Ollama
through the backend. Local GGUF files and the LM Studio API are also available;
LM Studio model load accepts a context size and optional user-selected skill and
MCP sources. The backend clears project conversations on restart and offloads
models after ten idle minutes.
The backend supplies planning, project-map,
file, command, skill, browser, and opt-in MCP tools with per-chat approvals.

Build instructions and IDE installation steps are in each adapter's README.
