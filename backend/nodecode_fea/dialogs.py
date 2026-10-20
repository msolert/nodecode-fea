"""Native "Open file" dialog, shown by the backend on the user's own desktop.

A web page can't learn the full path of a file the user picks, but the backend runs
on the same machine, so it can show the operating system's dialog itself.
Dialogs run in a child process: GUI toolkits want to own the main thread, and a
crashed or missing toolkit then can't take the server down.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

_TK_SCRIPT = r"""
import sys
try:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
except Exception:
    sys.exit(3)
root.withdraw()
root.attributes("-topmost", True)
root.update()
patterns = " ".join("*" + e for e in sys.argv[3].split(","))
path = filedialog.askopenfilename(
    parent=root, title=sys.argv[2], initialdir=sys.argv[1] or None,
    filetypes=[(sys.argv[2][5:], patterns), ("All files", "*.*")])
root.destroy()
sys.stdout.write(path or "")
"""

UNAVAILABLE = 3


def _initial_dir(current: str) -> str:
    if current and not current.startswith("mock://"):
        d = os.path.dirname(current)
        if os.path.isdir(d):
            return d
    return os.path.expanduser("~")


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired):
        return UNAVAILABLE, ""
    return proc.returncode, proc.stdout.strip()


def file_title(extensions: list[str]) -> str:
    """The dialog title for files with these extensions (default: ODBs)."""
    return "Open Abaqus ODB" if extensions == [".odb"] else "Open " + ", ".join(e.lstrip(".").upper() for e in extensions)


def pick_odb(current: str = "", extensions: list[str] | None = None) -> dict:
    """Show a native file dialog for files with these extensions (default .odb).
    Returns {"available": bool, "path": str | None}."""
    extensions = extensions or [".odb"]
    title = file_title(extensions)
    patterns = " ".join("*" + e for e in extensions)
    start = _initial_dir(current)
    # Linux desktops: prefer the desktop's own dialog over Tk's.
    if sys.platform.startswith("linux") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        if shutil.which("zenity"):
            code, out = _run(["zenity", "--file-selection", f"--title={title}",
                              f"--filename={start}/", f"--file-filter={title[5:]} | {patterns}", "--file-filter=All | *"])
            if code in (0, 1):  # 1 = cancelled
                return {"available": True, "path": out or None}
        if shutil.which("kdialog"):
            code, out = _run(["kdialog", "--getopenfilename", start, patterns])
            if code in (0, 1):
                return {"available": True, "path": out or None}
    code, out = _run([sys.executable, "-c", _TK_SCRIPT, start, title, ",".join(extensions)])
    if code != 0:  # no tkinter, no display, or the toolkit crashed
        return {"available": False, "path": None}
    return {"available": True, "path": out or None}
