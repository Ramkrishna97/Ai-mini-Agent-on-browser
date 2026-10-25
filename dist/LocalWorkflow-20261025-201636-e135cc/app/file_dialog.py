"""Run a native file dialog in a dedicated process so Tk owns its main thread."""

import json
import sys


def main():
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        types = [
            ("Supported reference files", "*.py *.pyw *.java *.js *.jsx *.ts *.tsx *.json *.css *.scss *.html *.htm *.cs *.cpp *.c *.h *.ps1 *.sql *.md *.txt *.csv *.yaml *.yml *.toml *.ini *.xml *.svg *.sh *.bat *.cmd *.go *.rs *.php *.rb *.kt *.ipynb *.log *.pdf *.docx"),
            ("Python", "*.py"), ("HTML", "*.html"), ("Text", "*.txt"), ("All files", "*.*"),
        ]
        if sys.argv[1:] == ["folder"]:
            path = filedialog.askdirectory(parent=root, title="Choose a workspace or project folder", mustexist=True)
        elif sys.argv[1:] == ["gguf"]:
            path = filedialog.askopenfilename(parent=root, title="Choose a local GGUF model",
                                              filetypes=[("GGUF models", "*.gguf"), ("All files", "*.*")])
        elif sys.argv[1:] == ["attachments"]:
            paths = filedialog.askopenfilenames(parent=root, title="Add read-only reference files", filetypes=types)
            print(json.dumps({"paths": list(paths)}))
            return 0
        else:
            path = filedialog.asksaveasfilename(
                parent=root, title="Choose an existing file or enter a new filename",
                filetypes=types, confirmoverwrite=False,
            )
        print(json.dumps({"path": path}))
    except Exception as exc:
        print(json.dumps({"error": f"Could not open the Windows file dialog: {exc}"}))
        return 1
    finally:
        if root is not None:
            root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
