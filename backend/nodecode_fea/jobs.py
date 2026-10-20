"""Run Abaqus analysis jobs from the editor and follow them through the files they write.

CAE only defines a Job and writes its input file (``<name>.inp``) as the model changes; it never submits one on its
own. Running is done here, when the Job node's Run is pressed (or automatically, when the node asks for it and the
input is newer than the results): one job at a time, in the order asked, with ``abaqus job=<name> interactive``.

A job's state is read from its directory, so it is right even for runs started elsewhere (CAE's Job Manager, a
previous editor session): ``.lck`` while Abaqus runs it, ``.sta`` for progress, the log for how it ended, and the
``.odb`` / ``.inp`` times for whether the results are older than the model.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .sources.abaqus import abaqus_command

DEFAULT_DIRECTORY = Path.home() / "nodecode-fea" / "jobs"
if not DEFAULT_DIRECTORY.exists() and (Path.home() / "abaqus-nocode" / "jobs").exists():
    DEFAULT_DIRECTORY = Path.home() / "abaqus-nocode" / "jobs"  # jobs run before the rename to NodeCode FEA
SETTLE = 2.0  # seconds an input file must be left alone before an automatic run reads it (CAE may still write it)


@dataclass(frozen=True)
class JobSpec:
    name: str
    directory: str
    cpus: int = 1
    memory: int = 90
    auto: bool = False
    user: str = ""  # a user subroutine file (.f / .for / .obj) compiled into the run
    check: bool = False  # Abaqus' data check of the input, as job <name>-Check (its files apart from the results)

    @property
    def key(self) -> str:
        return os.path.normcase(os.path.join(self.directory, self.name)) + ("|check" if self.check else "")

    def file(self, ext: str) -> str:
        return os.path.join(self.directory, self.name + ext)

    def check_file(self, ext: str) -> str:
        return os.path.join(self.directory, self.name + CHECK + ext)


CHECK = "-Check"


def spec_of(job) -> JobSpec:
    """The JobSpec of a Job value."""
    return JobSpec(job.name, job.directory, job.cpus, job.memory, job.action == "Run automatically",
                   getattr(job, "user", ""))


def read_text(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode(errors="replace")
    except OSError:
        return ""


def _mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _tail(path: str, size: int = 20000) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - size))
            return fh.read().decode(errors="replace")
    except OSError:
        return ""


def _float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def parse_sta(sta: str, attempts: bool = False) -> list[dict]:
    """The increments a .sta file lists, oldest first: step, increment, attempts (cutbacks), iterations, total
    time, step time and time increment. Abaqus/Standard writes one line per increment ('1U' attempts were cut
    back and retried); Abaqus/Explicit writes increment, total time, … under a 'STEP n' heading."""
    rows, step = [], 1
    for line in sta.splitlines():
        cols = line.split()
        heading = re.match(r"\s*STEP\s+(\d+)\b", line)
        if heading:
            step = int(heading.group(1))
            continue
        if len(cols) >= 9 and cols[0].isdigit() and cols[1].isdigit() and re.fullmatch(r"\d+U?", cols[2]):
            nums = [_float(c) for c in cols[3:9]]
            if None in nums:
                continue
            severe, equil, total, time_total, time_step, inc = nums
            rows.append({"step": int(cols[0]), "increment": int(cols[1]), "attempts": int(cols[2].rstrip("U")),
                         "cutback": cols[2].endswith("U"), "severe": int(severe), "equilibrium": int(equil),
                         "iterations": int(total), "total time": time_total, "step time": time_step,
                         "time increment": inc})
        elif len(cols) >= 3 and cols[0].isdigit() and "E" in cols[1].upper() and _float(cols[1]) is not None:
            # increment, step time, total time, CPU / wallclock time (hh:mm:ss), stable increment, …
            total = _float(cols[2]) if "E" in cols[2].upper() else None
            stable = next((_float(c) for c in cols[3:6] if "E" in c.upper() and _float(c) is not None), None)
            rows.append({"step": step, "increment": int(cols[0]), "attempts": 1, "cutback": False,
                         "severe": 0, "equilibrium": 0, "iterations": 0,
                         "total time": _float(cols[1]) if total is None else total, "step time": _float(cols[1]),
                         "time increment": stable})
    if attempts:
        return rows
    # the increments that converged: a cut-back attempt is retried under the same number
    return [r for r in rows if not r["cutback"]] or rows


def progress(sta: str) -> str:
    """'Step 2, increment 14, total time 1.35' from the last increment of a .sta file."""
    rows = parse_sta(sta)
    if not rows:
        return ""
    r = rows[-1]
    return f"Step {r['step']}, increment {r['increment']}, total time {r['total time']:g}"


def problem_sets(spec: "JobSpec") -> list[str]:
    """The node/element sets Abaqus says it put what it warned about or failed on in ("… identified in element set
    ErrElemExcessDistortion"); they are in the ODB."""
    found = []
    for ext in (".msg", ".dat"):
        for name in re.findall(r"(?:node|element)\s+set\s+((?:Warn|Err)\w+)", read_text(spec.file(ext)), re.I):
            if name not in found:
                found.append(name)
    return found


# What a solver message means and what usually helps (matched in the .msg/.dat of a run, upper case).
ADVICE = [
    ("TOO MANY ATTEMPTS MADE FOR THIS INCREMENT",
     "an increment did not converge even after cutting it back: the last increments of Messages (.msg) say where "
     "the largest residuals are"),
    ("TIME INCREMENT REQUIRED IS LESS THAN THE MINIMUM",
     "the time increment fell below the Step's minimum: lower Min increment, or find what keeps it from converging"),
    ("ZERO PIVOT", "zero pivot: a part can move as a rigid body (a missing boundary condition, a contact not yet "
                   "closed, a tie or coupling missing); hold it, or stabilize the contact"),
    ("NUMERICAL SINGULARITY", "numerical singularity: something is free to move without resistance (a missing "
                              "boundary condition or connection, an unconstrained degree of freedom)"),
    ("NEGATIVE EIGENVALUE", "negative eigenvalues: an instability (buckling, snap-through, softening material or "
                            "chattering contact); use Riks or stabilization if it is real"),
    ("EXCESSIVE DISTORTION", "elements are excessively distorted: refine the mesh there, apply the load more "
                             "gradually (smaller max increment), or use better suited elements"),
    ("OVERCONSTRAINT", "overconstraints: a node is held twice (boundary condition with a tie, coupling or contact "
                       "on the same degrees of freedom)"),
    ("FIFTY TIMES THE STRAIN TO CAUSE FIRST YIELD", "large plastic strain in one increment: lower the Step's max "
                                                    "increment"),
    ("OVERCLOSURE", "contact overclosures: surfaces start inside each other; move them apart or let the "
                    "Interaction adjust them"),
    ("DEFORMATION SPEED EXCEEDS", "elements deform faster than the wave speed (Explicit): the load is too fast or "
                                  "the mesh too distorted"),
]


def diagnose(spec: "JobSpec") -> list[str]:
    """How the run is converging, from its .sta and messages: cutbacks, a collapsing time increment, the
    solver's known complaints (with what usually helps) and where the largest residual was last."""
    out = []
    rows = parse_sta(read_text(spec.file(".sta")), attempts=True)
    cut = [r for r in rows if r["cutback"]]
    done = [r for r in rows if not r["cutback"]]
    if len(cut) >= 3:
        out.append(f"{len(cut)} cutbacks in {len(done)} increments: Abaqus keeps retrying with smaller increments "
                   "(contact, material or geometric nonlinearity it struggles with)")
    if done and done[-1]["time increment"]:
        last = done[-1]
        biggest = max((r["time increment"] or 0) for r in done if r["step"] == last["step"])
        if biggest and last["time increment"] < 1e-3 * biggest:
            out.append(f"The time increment fell to {last['time increment']:.3g} in step {last['step']} (from "
                       f"{biggest:.3g}): the analysis is close to stalling")
    text = _tail(spec.file(".msg"), 2_000_000) + _tail(spec.file(".dat"), 500_000)
    upper = text.upper()
    for key, advice in ADVICE:
        if key in upper:
            out.append(advice[0].upper() + advice[1:])
    residual = None
    for m in re.finditer(r"LARGEST RESIDUAL FORCE\s+(\S+)\s+AT NODE\s+(\d+)\s+DOF\s+(\d+)"
                         r"(?:\s+INSTANCE:?\s+(\S+))?", text, re.I):
        residual = m
    if residual is not None and any(k in upper for k in ("TOO MANY ATTEMPTS", "LESS THAN THE MINIMUM")):
        where = f"node {residual.group(2)}" + (f" of {residual.group(4)}" if residual.group(4) else "")
        out.append(f"The largest residual force when it stopped was {residual.group(1)} at {where}, degree of "
                   f"freedom {residual.group(3)}: look at what holds or loads it")
    return out


FILES = {"Status (.sta)": ".sta", "Messages (.msg)": ".msg", "Data (.dat)": ".dat", "Log (.log)": ".log"}
TAIL = 200_000  # characters of a job file shown (.dat and .msg files can be very large)


def file_text(path: str, size: int = TAIL) -> str:
    """A job file's text, its end only when it is long (a running job keeps adding to it)."""
    try:
        if os.path.getsize(path) <= size:
            return read_text(path)
    except OSError:
        return ""
    text = _tail(path, size)
    return "[… start of the file left out …]\n" + text.split("\n", 1)[-1]


def live(odb_path: str) -> bool:
    """The job that writes this ODB is running (Abaqus holds its .lck file)."""
    return os.path.exists(os.path.splitext(odb_path)[0] + ".lck")


def _errors(spec: JobSpec) -> list[str]:
    found = []
    for ext in (".log", ".msg", ".dat"):
        for line in _tail(spec.file(ext)).splitlines():
            line = line.strip()
            if re.search(r"\*\*\*ERROR|Abaqus Error|exited with error", line, re.I) and line not in found:
                found.append(line)
    return found[:6]


class JobRunner:
    def __init__(self):
        self.lock = threading.Lock()
        self.queue: list[JobSpec] = []
        self.current: tuple[JobSpec, subprocess.Popen] | None = None
        self.killed: set[str] = set()
        self.watched: dict[str, JobSpec] = {}
        self.checks: dict[str, JobSpec] = {}
        self.live_odbs: dict[str, str] = {}  # running jobs' ODBs opened for results (key: normalised path)
        self.tried: dict[str, float | None] = {}  # input file time of each job's last run (automatic runs: once)
        self.revision = 0
        self.live_revision = 0  # moves only when a followed running job's ODB gets more results, or its job ends
        self._seen: dict[str, tuple] = {}
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- queries
    def watch(self, spec: JobSpec):
        """A Job node evaluated: follow this job (and run it automatically when it asks)."""
        with self.lock:
            self.watched[spec.key] = spec

    def follow_odb(self, path: str):
        """An ODB was opened while its job runs: its readers update as the job writes more increments."""
        with self.lock:
            self.live_odbs[os.path.normcase(os.path.abspath(path))] = path

    def status(self, spec: JobSpec) -> dict:
        with self.lock:
            running = self.current is not None and self.current[0].key == spec.key
            queued = any(q.key == spec.key for q in self.queue)
            killed = spec.key in self.killed
        inp, odb, lck = _mtime(spec.file(".inp")), _mtime(spec.file(".odb")), os.path.exists(spec.file(".lck"))
        log = _tail(spec.file(".log"))
        sta = _tail(spec.file(".sta"))
        out = {"name": spec.name, "directory": spec.directory, "odb": spec.file(".odb"), "progress": "",
               "errors": [], "stale": False}
        if running and killed:
            out.update(state="Stopping", progress=progress(sta))
        elif running or lck:
            out.update(state="Running", progress=progress(sta))
        elif queued:
            out["state"] = "Queued"
        elif killed:
            out["state"] = "Killed"
        elif "COMPLETED" in log or "HAS COMPLETED SUCCESSFULLY" in sta:
            out["state"] = "Completed"
        elif log.strip() or "NOT BEEN COMPLETED" in sta:
            out.update(state="Failed", errors=_errors(spec) or log.strip().splitlines()[-3:])
        elif odb is not None:
            out["state"] = "Has results"
        elif inp is not None:
            out["state"] = "Input written"
        else:
            out["state"] = "No input yet"
        # The model changed since this run: CAE rewrote the input after the results (or the failed run's log).
        ran = odb if out["state"] in ("Completed", "Has results") else (
            _mtime(spec.file(".log")) if out["state"] in ("Failed", "Killed") else None)
        out["stale"] = inp is not None and ran is not None and inp > ran + 1e-3
        return out

    def watch_check(self, spec: JobSpec):
        """Follow this job's data check too (the editor re-runs the graph when it ends)."""
        with self.lock:
            self.checks[spec.key] = spec

    def checked(self, spec: JobSpec) -> bool:
        """A data check of this job has finished (its .dat exists and isn't being written)."""
        check = replace(spec, check=True)
        with self.lock:
            busy = (self.current is not None and self.current[0].key == check.key) or any(
                q.key == check.key for q in self.queue)
        return not busy and os.path.exists(spec.check_file(".dat"))

    def check_stale(self, spec: JobSpec) -> bool:
        """The input was written again after the last data check."""
        inp, dat = _mtime(spec.file(".inp")), _mtime(spec.check_file(".dat"))
        return inp is not None and dat is not None and inp > dat + 1e-3

    def check_state(self, spec: JobSpec) -> str:
        check = replace(spec, check=True)
        with self.lock:
            if self.current is not None and self.current[0].key == check.key:
                return "Checking…"
            if any(q.key == check.key for q in self.queue):
                return "Check queued"
        if not os.path.exists(spec.check_file(".dat")):
            return ""
        text = read_text(spec.check_file(".dat")) + read_text(spec.check_file(".msg"))
        errors, warnings = text.count("***ERROR"), text.count("***WARNING")
        state = ("Checked: no errors" if not errors else f"Checked: {errors} error{'s' if errors != 1 else ''}")
        if warnings:
            state += f", {warnings} warning{'s' if warnings != 1 else ''}"
        return state + (" (model changed since)" if self.check_stale(spec) else "")

    def poll(self) -> dict:
        """Status of every watched job; starts automatic runs; the revision moves when anything changed."""
        with self.lock:
            specs = list(self.watched.values())
        now = time.time()
        states = {}
        for spec in specs:
            st = self.status(spec)
            states[spec.key] = (st["state"], st["progress"], st["stale"], _mtime(spec.file(".odb")))
            inp = _mtime(spec.file(".inp"))
            if (spec.auto and inp is not None and now - inp > SETTLE and self.tried.get(spec.key) != inp and
                    (st["state"] == "Input written" or st["stale"])):
                self.run([spec])
        with self.lock:
            check_specs = list(self.checks.values())
        for spec in check_specs:
            states[spec.key + "|check"] = (self.check_state(spec), _mtime(spec.check_file(".dat")))
        with self.lock:
            odbs = dict(self.live_odbs)
        for key, path in odbs.items():
            stem = os.path.splitext(path)[0]
            running = live(path)
            states[key + "|odb"] = ("Running" if running else "Ended", _mtime(path), _mtime(stem + ".sta"))
            if not running:  # its final results are read once more (the revision moves), then it isn't followed
                with self.lock:
                    self.live_odbs.pop(key, None)
        with self.lock:
            if states != {k: v for k, v in self._seen.items() if k in states}:
                self.revision += 1
            if any(v != self._seen.get(k) for k, v in states.items() if k.endswith("|odb")):
                self.live_revision += 1
            self._seen = states
            active = sum(1 for s in states.values() if s[0] in ("Running", "Queued", "Stopping", "Checking…",
                                                                 "Check queued"))
            return {"revision": self.revision, "active": active, "results": self.live_revision}

    # ------------------------------------------------------------- actions
    def run(self, specs: list[JobSpec], check: bool = False) -> list[str]:
        """Queue these jobs (each once), or their data checks; returns what was queued or why not."""
        notes = []
        if check:
            specs = [replace(s, check=True, auto=False) for s in specs]
        with self.lock:
            for spec in specs:
                if not spec.check:
                    self.watched.setdefault(spec.key, spec)
                if not os.path.exists(spec.file(".inp")):
                    notes.append(f"{spec.name}: no input file yet (CAE writes it once the model is built)")
                    continue
                if (self.current and self.current[0].key == spec.key) or any(q.key == spec.key for q in self.queue):
                    notes.append(f"{spec.name}: already running or queued")
                    continue
                if os.path.exists(spec.file(".lck")):
                    notes.append(f"{spec.name}: Abaqus is already running it (its .lck file exists)")
                    continue
                self.tried[spec.key] = _mtime(spec.file(".inp"))
                self.killed.discard(spec.key)
                self.queue.append(spec)
                notes.append(f"{spec.name}: {'data check ' if spec.check else ''}queued")
            self.revision += 1
            if self.queue and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._work, daemon=True)
                self._thread.start()
        return notes

    def kill(self, specs: list[JobSpec]) -> list[str]:
        notes, terminate = [], None
        with self.lock:
            for spec in specs:
                before = len(self.queue)
                self.queue = [q for q in self.queue if q.key != spec.key]
                if len(self.queue) != before:
                    self.killed.add(spec.key)
                    notes.append(f"{spec.name}: removed from the queue")
                elif self.current and self.current[0].key == spec.key:
                    self.killed.add(spec.key)
                    terminate = self.current
                    notes.append(f"{spec.name}: stopping")
                else:
                    notes.append(f"{spec.name}: not running")
            self.revision += 1
        if terminate is not None:  # Abaqus takes a while to stop: don't hold up the editor
            threading.Thread(target=self._terminate, args=terminate, daemon=True).start()
        return notes

    @staticmethod
    def _terminate(spec: JobSpec, proc: subprocess.Popen):
        """The launcher starts the solver as another process: ask Abaqus to stop it, then end the launcher."""
        try:
            subprocess.run([abaqus_command(), "terminate", f"job={spec.name}"], cwd=spec.directory,
                           capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            proc.wait(60)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _work(self):
        while True:
            with self.lock:
                if not self.queue:
                    self.current = None
                    self.revision += 1
                    return
                spec = self.queue.pop(0)
            if spec.check:  # the solver isn't run: the input is read and checked (.dat, .msg)
                for ext in (".dat", ".msg"):
                    try:
                        os.remove(spec.check_file(ext))
                    except OSError:
                        pass
                cmd = [abaqus_command(), f"job={spec.name}{CHECK}", f"input={spec.name}.inp", "datacheck",
                       "ask_delete=OFF", "interactive"]
            else:
                cmd = [abaqus_command(), f"job={spec.name}", f"input={spec.name}.inp", f"cpus={spec.cpus}",
                       f"memory={spec.memory} %", "ask_delete=OFF", "interactive"] + (
                    [f"user={spec.user}"] if spec.user else [])
            logfile = spec.check_file(".log") if spec.check else spec.file(".log")
            try:
                with open(logfile, "w") as log:
                    proc = subprocess.Popen(cmd, cwd=spec.directory, stdin=subprocess.DEVNULL, stdout=log,
                                            stderr=subprocess.STDOUT)
                    with self.lock:
                        self.current = (spec, proc)
                        self.revision += 1
                    proc.wait()
            except OSError as exc:
                with open(logfile, "a") as log:
                    log.write(f"Abaqus Error: could not start '{cmd[0]}': {exc}\n")
            with self.lock:
                self.current = None
                self.revision += 1


runner = JobRunner()
