"""Real ODBs, read through a long-lived ``abaqus python odb_worker.py`` process."""

from __future__ import annotations

import base64
import itertools
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import numpy as np

from .base import ResultSource, SourceError

PREFIX = "@@NOCODE@@"
WORKER = Path(__file__).resolve().parent.parent / "worker" / "odb_worker.py"


# Where SIMULIA installs its launcher on Windows, when it isn't on PATH and ABAQUS_CMD isn't set.
DEFAULT_LAUNCHERS = ("C:/SIMULIA/Commands/abaqus.bat",)


def abaqus_command() -> str:
    """The Abaqus launcher as a full path when it can be found (ABAQUS_CMD, else 'abaqus' on PATH, else SIMULIA's
    default folder). A full path matters on Windows: subprocess doesn't add '.bat' to a bare 'abaqus', so a job or
    data check would fail to start ('WinError 2') even though CAE, launched through shutil.which, opens."""
    name = os.environ.get("ABAQUS_CMD", "abaqus")
    found = shutil.which(name)
    if found:
        return found
    if "ABAQUS_CMD" not in os.environ:
        for path in DEFAULT_LAUNCHERS:
            if os.path.isfile(path):
                return path
    return name


def abaqus_available() -> bool:
    return shutil.which(abaqus_command()) is not None


def _decode(obj):
    if isinstance(obj, dict):
        if "__nd__" in obj:
            raw = base64.b64decode(obj["b64"])
            return np.frombuffer(raw, dtype=np.dtype(obj["__nd__"])).reshape(obj["shape"]).copy()
        return {k: _decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode(v) for v in obj]
    return obj


def _expand_instances(res: dict) -> dict:
    names = res.pop("instanceNames")
    index = res.pop("instanceIndex")
    res["instances"] = np.array(names, dtype=object)[index] if len(names) else np.zeros(0, dtype=object)
    return res


class Worker:
    """One worker process shared by every real ODB; requests are serialised."""

    _instance: "Worker | None" = None
    _guard = threading.Lock()

    @classmethod
    def get(cls) -> "Worker":
        with cls._guard:
            if cls._instance is None or cls._instance.proc.poll() is not None:
                cls._instance = Worker()
            return cls._instance

    def __init__(self):
        exe = shutil.which(abaqus_command())
        if exe is None:
            raise SourceError(
                f"Abaqus command '{abaqus_command()}' not found on PATH. "
                "Set ABAQUS_CMD to your Abaqus launcher, or use a mock:// path.")
        self.proc = subprocess.Popen(
            [exe, "python", str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.log: list[str] = []
        self._read(0)  # wait for the ready message

    def _read(self, rid):
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            if not line.startswith(PREFIX):
                self.log = (self.log + [line.rstrip()])[-50:]
                continue
            msg = json.loads(line[len(PREFIX):])
            if msg.get("id") == rid:
                return msg
        tail = "\n".join(self.log[-10:])
        raise SourceError(f"Abaqus worker exited unexpectedly.\n{tail}")

    def call(self, method: str, **params):
        with self.lock:
            rid = next(self.ids)
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps({"id": rid, "method": method, "params": params}) + "\n")
            self.proc.stdin.flush()
            msg = self._read(rid)
        if "error" in msg:
            raise SourceError(msg["error"])
        return _decode(msg["result"])


class AbaqusSource(ResultSource):
    def __init__(self, path: str):
        self.path = path
        if not Path(path).exists():
            raise SourceError(f"ODB not found: {path}")
        self.worker = Worker.get()

    def describe(self):
        return self.worker.call("describe", path=self.path)

    def field(self, step, frame, variable, position="Automatic", section_point=0,
              complex_part="Real", phase_angle=0.0):
        res = _expand_instances(self.worker.call("field", path=self.path, step=step, frame=frame, variable=variable,
                                                 position=position, section_point=section_point,
                                                 complex_part=complex_part, phase_angle=phase_angle))
        res["values"] = res["values"].reshape(len(res["labels"]), -1)
        return res

    def set(self, name):
        return _expand_instances(self.worker.call("set", path=self.path, name=name))

    def history(self, step, region, output):
        return self.worker.call("history", path=self.path, step=step, region=region, output=output)

    def mesh(self):
        return self.worker.call("mesh", path=self.path)

    def close(self):
        try:
            self.worker.call("close", path=self.path)
        except SourceError:
            pass
