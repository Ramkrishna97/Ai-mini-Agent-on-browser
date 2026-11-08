"""Copy the same small backend source snapshot into an IDE plugin bundle."""

from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parent.parent
ASSETS = ("index.html", "ui.js", "ui.css", "requirements.txt", "requirements-npu.txt", "core_tool_manifest.json",
          "mcp_servers.example.json")


def main():
    target = Path(sys.argv[1]).resolve()
    target.mkdir(parents=True, exist_ok=True)
    names = [path.name for path in ROOT.glob("*.py")] + list(ASSETS)
    for name in names:
        shutil.copy2(ROOT / name, target / name)
    (target / "backend-files.txt").write_text("\n".join(names) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
