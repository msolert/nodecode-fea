"""Job: writing the input file, submitting and monitoring the analysis."""

from __future__ import annotations

import os
import numpy as np

from ... import jobs
from ...values import AnalysisStep, Condition, Interaction, Job, Model, Series, Text
from ..core import Param, Port, register
from ..common import _flat_items
from .geometry import _abaqus_name, _numbered


JOB_RUN = ["Run on request", "Run automatically", "Write input only"]
# Older graphs: CAE submitted the job itself on every model change.
_LEGACY_JOB_RUN = {"Write input": "Write input only", "Submit": "Run on request", "Submit and wait": "Run automatically"}


def _job_spec(job: Job) -> "jobs.JobSpec":
    return jobs.spec_of(job)


JOB_SHOW = ["State", *jobs.FILES]


def _job_file(job: Job, show: str) -> str:
    """The job file a Show option names ('' for State)."""
    if show not in JOB_SHOW:
        raise ValueError(f"Show is one of {', '.join(JOB_SHOW)}")
    return jobs.spec_of(job).file(jobs.FILES[show]) if show != "State" else ""


def _job_text(job: Job, show: str) -> Text:
    """The text of the job file Show picks (State: the status file, .sta), read as the job writes it."""
    path = _job_file(job, show) or jobs.spec_of(job).file(".sta")
    return Text(jobs.file_text(path), os.path.basename(path))


def _job_increments(job: Job) -> list[Series]:
    """The job's increments from its .sta file, as curves over the increment count: total time and time increment
    (how the solver is getting on; a time increment that keeps shrinking is a convergence problem)."""
    rows = jobs.parse_sta(jobs.read_text(jobs.spec_of(job).file(".sta")))
    x = np.arange(1, len(rows) + 1, dtype=float)
    curves = [Series(f"{job.name} total time", x, np.array([r["total time"] for r in rows], dtype=float),
                     "Increment", "Total time")]
    if any(r["time increment"] is not None for r in rows):
        curves.append(Series(f"{job.name} time increment", x,
                             np.array([np.nan if r["time increment"] is None else r["time increment"]
                                       for r in rows], dtype=float), "Increment", "Time increment"))
    return curves


def _job_display(out: dict, show: str = "State") -> dict:
    """Each job's state, read from its files: input written, queued, running (progress), completed, failed; or
    the file Show picks (.sta, .msg, .dat, .log), as the job writes it."""
    items = [j for j in _flat_items(out.get("job")) if isinstance(j, Job)]
    if show != "State" and items:
        parts = []
        for job in items:
            path = _job_file(job, show)
            text = jobs.file_text(path)
            if not text:
                text = (f"No {os.path.basename(path)} yet" + (": Abaqus writes it once the job runs"
                                                              if show != "Log (.log)" else ""))
            parts.append(text if len(items) == 1 else f"===== {job.name} =====\n{text}")
        title = os.path.basename(_job_file(items[0], show)) if len(items) == 1 else f"{show} of {len(items)} jobs"
        state = jobs.runner.status(_job_spec(items[0]))["state"] if len(items) == 1 else ""
        return {"kind": "log", "title": title + (f" · {state}" if state else ""), "text": "\n".join(parts),
                "path": _job_file(items[0], show) if len(items) == 1 else ""}
    rows = []
    for job in items:
        st = jobs.runner.status(_job_spec(job))
        note = st["progress"] or "; ".join(st["errors"][:2])
        if st["stale"]:
            note = ("Model changed since this run. " + note).strip()
        check = jobs.runner.check_state(_job_spec(job))
        rows.append([job.name, st["state"] + (f" · {check}" if check else ""), note, st["odb"]])
    if len(rows) == 1:
        name, state, note, odb = rows[0]
        job = items[0]
        problems = jobs.problem_sets(_job_spec(job))
        return {"kind": "info", "title": f"Job {name}: {state}", "items": [
            ["Model", job.model.name], ["State", state], *([["Note", note]] if note else []),
            *([["Problem sets", ", ".join(problems) + " (in the ODB: Set Warn*, Err* shows them)"]]
              if problems else []),
            ["Runs", job.action], ["CPUs", str(job.cpus)], ["ODB", odb]]}
    return {"kind": "table", "title": f"{len(rows)} jobs", "columns": ["Job", "State", "Note", "ODB"],
            "rows": rows, "allRows": rows, "total": len(rows), "stats": []}


def _job_action(out: dict, action: str) -> list[str] | dict:
    specs = [_job_spec(j) for j in _flat_items(out.get("job")) if isinstance(j, Job)]
    if not specs:
        raise ValueError("No job to run: fix the Job node first")
    if action == "Results":  # post-processing: the editor wires the Job into Open ODB and shows its Results view
        found = [s.file(".odb") for s in specs if os.path.exists(s.file(".odb"))]
        if not found:
            raise ValueError("No results yet: Run the job first" if len(specs) == 1 else "None of these jobs has "
                             "results yet: Run them first")
        missing = [s.name for s in specs if not os.path.exists(s.file(".odb"))]
        return {"notes": [f"Results of {', '.join(os.path.splitext(os.path.basename(f))[0] for f in found)} "
                          "in the Results view (Job → Open ODB)"] + ([f"no results yet: {', '.join(missing)}"] if missing else []),
                "open": found}
    if action == "Check":
        return jobs.runner.run(specs, check=True)
    return jobs.runner.run(specs) if action == "Run" else jobs.runner.kill(specs)


@register("model.job", "Job", "Model",
          "Create an Abaqus analysis job (solve, submit, run an analysis) for a Model or the last Step, Load / "
          "Boundary Condition, or Interaction. CAE writes its input file as the model changes; Run solves it (one "
          "job at a time, a list of jobs in turn) and Kill stops it. The node shows the state and progress, and "
          "whether the model changed since the run. Check runs Abaqus' data check (validate, verify, datacheck) "
          "without solving and puts its errors and warnings on the nodes they concern. Results wires it into Open "
          "ODB and shows the Results view (post-process, view the results of the finished job). Show reads the "
          "files the "
          "job writes as it runs (monitor: status .sta, messages .msg, data .dat, log); File text outputs it and "
          "Increments gives total time and time increment per increment (convergence). ODB path connects to Open "
          "ODB, which reads the results written so far while the job runs and updates with each increment. "
          "Lists of models, names or resources make parameter-study jobs.",
          inputs=[Port("analysis", "any", "Model or analysis", depth=0)],
          outputs=[Port("path", "text", "ODB path"), Port("job", "job", "Job"), Port("text", "text", "File text"),
                   Port("increments", "series", "Increments")],
          params=[Param("name", "text", "Job-1", "Name"),
                  Param("action", "select", "Run on request", "Run", options=JOB_RUN,
                        help="Run on request: only when you press Run. Run automatically: whenever CAE has written "
                             "a newer input than the results (not while it runs). Write input only: never run."),
                  Param("control", "action", "", "", options=["Check", "Run", "Kill", "Results"],
                        help="Check: Abaqus' data check of the input (no solve); its errors and warnings show on "
                             "the nodes they concern. Run: solve. Kill: stop. Results: wire it into Open ODB and "
                             "show the Results view (post-processing), also while it runs."),
                  Param("directory", "text", "", "Working directory",
                        help=f"Where the job's files go. Empty: {jobs.DEFAULT_DIRECTORY}"),
                  Param("cpus", "int", 1, "CPUs"),
                  Param("memory", "int", 90, "Memory (%)"),
                  Param("user", "text", "", "User subroutine",
                        help="A user subroutine file (.f, .for or .obj; Fortran needs a compiler set up for "
                             "Abaqus) compiled into the run: a UAMP / VUAMP for a User Amplitude, or any other. "
                             "Empty: none."),
                  Param("show", "select", "State", "Show", options=JOB_SHOW,
                        help="What the node shows: the job's state, or one of the files Abaqus writes as it runs "
                             "(status .sta: one line per increment; messages .msg: iterations, warnings, errors; "
                             "data .dat; log .log). File text outputs the same file (State: the .sta).")],
          display=lambda o, i, p: _job_display(o, str(p.get("show") or "State")), display_lists=True,
          action=_job_action,
          version=lambda p, i: jobs.runner.revision)
def model_job(inputs, params):
    source = inputs["analysis"]
    if isinstance(source, Model):
        model = source
    elif isinstance(source, AnalysisStep):
        model = source.model
    elif isinstance(source, (Condition, Interaction)):
        model = source.step.model
    else:
        raise ValueError("Job needs a Model, Step, Load / Boundary Condition, or Interaction")
    cpus, memory = int(params["cpus"]), int(params["memory"])
    if cpus < 1:
        raise ValueError("CPUs must be at least 1")
    if not 1 <= memory <= 100:
        raise ValueError("Memory must be between 1 and 100 percent")
    name = _numbered(_abaqus_name(str(params["name"]), "Job-1"), params)
    typed = str(params.get("directory") or "").strip()
    directory = os.path.abspath(os.path.expanduser(typed)) if typed not in ("", ".") else str(jobs.DEFAULT_DIRECTORY)
    run = _LEGACY_JOB_RUN.get(str(params["action"]), str(params["action"]))
    if run not in JOB_RUN:
        raise ValueError(f"Unknown Run option '{params['action']}'")
    user = str(params.get("user") or "").strip().strip('"')
    if user:
        user = os.path.abspath(os.path.expanduser(user))
        if not os.path.isfile(user):
            raise ValueError(f"User subroutine: no file {user}")
        if os.path.splitext(user)[1].lower() not in (".f", ".for", ".f90", ".obj", ".o", ".c", ".cpp"):
            raise ValueError("User subroutine is a Fortran (.f, .for), C/C++ or object (.obj, .o) file")
    job = Job(name, model, source, run, directory, cpus, memory, user)
    if run != "Write input only":
        jobs.runner.watch(_job_spec(job))
    jobs.runner.watch_check(_job_spec(job))
    return {"path": Text(os.path.join(directory, name + ".odb"), name), "job": job,
            "text": _job_text(job, str(params.get("show") or "State")), "increments": _job_increments(job)}
