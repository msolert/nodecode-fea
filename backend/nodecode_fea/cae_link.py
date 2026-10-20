"""Start Abaqus/CAE from the editor and talk to it while it runs.

Nothing is installed in Abaqus. Each launch gets a session folder
(~/.nodecode-fea/cae/<time>/) holding the link code; CAE is started there with
``abaqus cae script=nocode_boot.py``, and loads the GUI half of the link from the
folder's abaqus_plugins/ as it starts. Requests go in inbox/, results come back
in outbox/, and both halves log to link.log.
"""

from __future__ import annotations

import itertools
import json
import re
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .sources import abaqus_command

FILES = Path(__file__).resolve().parent / "cae_link_files"
MACHINE_OUTPUT = ("__NODECODE_FEA_MESH__", "__NODECODE_FEA_MEASURE__", "__NODECODE_FEA_OBJECTS__")


def sessions_root() -> Path:
    home = os.environ.get("NODECODE_FEA_HOME") or os.environ.get("ABAQUS_NOCODE_HOME")  # the name before NodeCode FEA
    return Path(home or Path.home() / ".nodecode-fea") / "cae"

HEARTBEAT_FRESH = 6.0  # seconds
STARTUP_GRACE = 180.0  # CAE can take a while to start (licence check, graphics)

BOOT = '''# Started by the NodeCode FEA editor: load the link's kernel half.
import sys
sys.path.insert(0, {session!r})
import nocode_kernel
nocode_kernel.boot()
'''


class LinkError(Exception):
    pass


class CaeLink:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.session: Path | None = None
        self.started = 0.0
        self.product = "cae"
        self.ids = itertools.count(1)
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- launch
    def launch(self, initial_script: str | None = None, product: str = "cae", module: str = "Visualization",
               sketch: tuple | list | None = None) -> dict:
        """Start Abaqus/CAE, or Abaqus/Viewer (product='viewer': results only, lighter), with the link. sketch:
        (model, name) the initial display opens from the Sketch module's Sketch list."""
        if product not in ("cae", "viewer"):
            raise LinkError(f"Unknown Abaqus product '{product}' (use cae or viewer)")
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                raise LinkError("CAE is already running from the editor")
            exe = shutil.which(abaqus_command())
            if exe is None:
                raise LinkError(f"Abaqus command '{abaqus_command()}' not found. Set ABAQUS_CMD to your Abaqus "
                                "launcher (e.g. C:\\SIMULIA\\Commands\\abaqus.bat) and restart.")
            root = sessions_root()
            session = root / time.strftime("%Y%m%d-%H%M%S")
            n = 1
            while session.exists():
                session = root / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
                n += 1
            (session / "inbox").mkdir(parents=True)
            (session / "outbox").mkdir()
            (session / "scripts").mkdir()
            plugins = session / "abaqus_plugins" / "nocode_link"
            plugins.mkdir(parents=True)
            shutil.copy(FILES / "nocode_link_plugin.py", plugins / "nocode_link_plugin.py")
            shutil.copy(FILES / "nocode_kernel.py", session / "nocode_kernel.py")
            (session / "nocode_boot.py").write_text(BOOT.format(session=str(session)), encoding="utf-8")
            if initial_script:
                (session / "initial.py").write_text(initial_script, encoding="utf-8")
                # the module, then the sketch to open in it (model and name), each on its own line
                (session / "initial_module.txt").write_text("\n".join([module, *(sketch or [])]), encoding="utf-8")

            kwargs: dict = {"cwd": session, "stdin": subprocess.DEVNULL,
                            "stdout": open(session / "cae_output.txt", "w"), "stderr": subprocess.STDOUT}
            if os.name == "nt":  # its own console group, so closing the editor doesn't close CAE
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            self.proc = subprocess.Popen([exe, product, "script=nocode_boot.py"], **kwargs)
            self.session, self.started, self.product = session, time.time(), product
            return self.status()

    def stop(self):
        """Used by tests; the editor never closes the user's CAE."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        if self.session is None:
            return {"state": "off"}
        alive = self.proc is not None and self.proc.poll() is None
        beat = self.session / "heartbeat"
        age = time.time() - beat.stat().st_mtime if beat.exists() else None
        if not alive:
            state = "closed"
        elif age is not None and age < HEARTBEAT_FRESH:
            state = "connected"
        elif time.time() - self.started < STARTUP_GRACE and age is None:
            state = "starting"
        else:
            state = "no-link"  # CAE runs, but the GUI half isn't answering
        return {"state": state, "session": str(self.session), "product": self.product,
                "kernelReady": (self.session / "kernel_ready").exists(),
                "log": self.log_tail()}

    def log_tail(self, lines: int = 200) -> list[str]:
        """CAE's start-up output, then the link log with what CAE reported for each update right under its request
        (what was built, unchanged or deleted, steps that failed, errors)."""
        if self.session is None:
            return []
        out = []
        p = self.session / "cae_output.txt"
        if p.exists():
            out += [f"[cae] {ln}" for ln in p.read_text(errors="replace").splitlines()[-20:] if ln.strip()]
        p = self.session / "link.log"
        if p.exists():
            for ln in p.read_text(errors="replace").splitlines():
                out.append(ln)
                m = re.search(r"request (\S+) \(exec\)", ln)
                if m:
                    out += self._reported(m.group(1))
        return out[-lines:]

    def _reported(self, rid: str) -> list[str]:
        """What one update printed in CAE, indented under its request line."""
        res = self.result(rid)
        if res is None:
            return ["    (no answer from CAE yet)"]
        text = [ln.replace("NodeCode FEA: ", "") for ln in (res.get("output") or "").splitlines()
                if ln.strip() and not ln.lstrip().startswith(MACHINE_OUTPUT)]
        if res.get("error"):
            text += ["ERROR: " + ln for ln in res["error"].strip().splitlines()[-6:]]
        return ["    " + ln for ln in text if ln != "done."]

    # -------------------------------------------------------------- requests
    def send(self, request: str, **payload) -> str:
        st = self.status()
        if st["state"] != "connected":
            raise LinkError({"off": "CAE isn't running. Click Launch CAE first.",
                             "starting": "CAE is still starting; try again in a moment.",
                             "closed": "CAE was closed. Launch it again.",
                             "no-link": "CAE is running but the live link isn't answering (see the link log)."}
                            [st["state"]])
        rid = f"r{int(time.time() * 1000)}-{next(self.ids)}"
        assert self.session is not None
        if request == "exec":
            script = self.session / "scripts" / f"{rid}.py"
            script.write_text(payload.pop("script"), encoding="utf-8")  # the scripts declare utf-8
            payload["script"] = str(script)
        tmp = self.session / "inbox" / f"{rid}.tmp"
        tmp.write_text(json.dumps({"id": rid, "type": request, **payload}))
        tmp.replace(self.session / "inbox" / f"{rid}.json")
        return rid

    def result(self, rid: str) -> dict | None:
        if self.session is None:
            return None
        p = self.session / "outbox" / f"{rid}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except ValueError:
            return None  # being written

    def wait(self, rid: str, timeout: float) -> dict | None:
        end = time.time() + timeout
        while time.time() < end:
            res = self.result(rid)
            if res is not None:
                return res
            time.sleep(0.1)
        return None


link = CaeLink()
