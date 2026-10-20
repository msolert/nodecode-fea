"""Running analysis jobs from the editor: queue, status from the job's files, kill, automatic runs, the API."""

import os
import time

import pytest
from fastapi.testclient import TestClient

from nodecode_fea import jobs
from nodecode_fea.app import app
from nodecode_fea.jobs import JobRunner, JobSpec
from nodecode_fea.sources.abaqus import Worker

from .fake_paths import LAUNCHER
from .test_engine import graph


@pytest.fixture(autouse=True)
def fake_abaqus(monkeypatch):
    monkeypatch.setenv("ABAQUS_CMD", str(LAUNCHER))
    monkeypatch.setenv("NOCODE_FAKE_JOB_SECONDS", "0.3")
    yield
    if Worker._instance is not None:  # Open ODB on a run's results started the fake ODB reader
        Worker._instance.proc.kill()
        Worker._instance = None


def spec(tmp_path, name="Solve", text="*HEADING\n", auto=False):
    (tmp_path / f"{name}.inp").write_text(text)
    return JobSpec(name, str(tmp_path), 1, 90, auto)


def wait_for(runner, s, states, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = runner.status(s)
        if st["state"] in states:
            return st
        time.sleep(0.05)
    raise AssertionError(f"{s.name} stayed {runner.status(s)['state']}")


def test_run_writes_results_and_reports_completion(tmp_path):
    r, s = JobRunner(), spec(tmp_path)
    assert r.status(s)["state"] == "Input written"
    assert r.run([s]) == ["Solve: queued"]
    st = wait_for(r, s, ("Completed",))
    assert not st["stale"] and os.path.exists(s.file(".odb")) and not os.path.exists(s.file(".lck"))
    # CAE rewrote the input since: the results are from an older model
    later = time.time() + 5
    os.utime(s.file(".inp"), (later, later))
    assert r.status(s)["stale"]


def test_failed_run_shows_the_error(tmp_path):
    r, s = JobRunner(), spec(tmp_path, text="*FAIL\n")
    r.run([s])
    st = wait_for(r, s, ("Failed",))
    assert any("***ERROR" in e for e in st["errors"])
    assert not st["stale"]  # the input it failed on is still the current one


def test_no_input_no_run_and_no_double_queue(tmp_path):
    r = JobRunner()
    missing = JobSpec("Nothing", str(tmp_path))
    assert "no input file yet" in r.run([missing])[0]
    s = spec(tmp_path)
    r.run([s])
    assert "already running or queued" in r.run([s])[0]
    wait_for(r, s, ("Completed",))


def test_kill_stops_a_running_job(tmp_path, monkeypatch):
    monkeypatch.setenv("NOCODE_FAKE_JOB_SECONDS", "6")
    r, s = JobRunner(), spec(tmp_path)
    r.run([s])
    wait_for(r, s, ("Running",))
    assert r.kill([s]) == ["Solve: stopping"]
    st = wait_for(r, s, ("Killed",))
    assert not os.path.exists(s.file(".odb")) and not os.path.exists(s.file(".lck"))
    assert st["state"] == "Killed"


def test_a_list_of_jobs_runs_one_at_a_time_in_order(tmp_path):
    r = JobRunner()
    specs = [spec(tmp_path, f"Study-{i}") for i in range(3)]
    assert r.run(specs) == [f"Study-{i}: queued" for i in range(3)]
    seen_running = set()
    end = time.time() + 30
    while time.time() < end and any(r.status(s)["state"] != "Completed" for s in specs):
        running = [s.name for s in specs if r.status(s)["state"] == "Running"]
        assert len(running) <= 1
        seen_running.update(running)
        time.sleep(0.02)
    assert all(r.status(s)["state"] == "Completed" for s in specs)
    times = [os.path.getmtime(s.file(".odb")) for s in specs]
    assert times == sorted(times)


def test_automatic_runs_once_per_input(tmp_path):
    r, s = JobRunner(), spec(tmp_path, auto=True)
    old = time.time() - 10  # CAE finished writing it a while ago
    os.utime(s.file(".inp"), (old, old))
    r.watch(s)
    r.poll()
    wait_for(r, s, ("Completed",))
    r.poll()
    assert r.status(s)["state"] == "Completed" and not r.queue
    # a new input (the model changed) runs again
    (tmp_path / "Solve.inp").write_text("*HEADING\nchanged\n")
    newer = time.time() + 5  # newer than the results, settled
    os.utime(s.file(".inp"), (newer, newer))
    r.status(s)
    monkey_now = jobs.SETTLE
    jobs.SETTLE = -10
    try:
        r.poll()
    finally:
        jobs.SETTLE = monkey_now
    assert r.queue or r.current
    wait_for(r, s, ("Completed",))


def test_job_node_buttons_and_status_through_the_api(tmp_path):
    (tmp_path / "Api-Job.inp").write_text("*HEADING\n")
    g = graph([("m", "model.model", {}),
               ("j", "model.job", {"name": "Api-Job", "directory": str(tmp_path)}),
               ("o", "odb.open", {})],
              [("m", "model", "j", "analysis"), ("j", "path", "o", "path")])
    client = TestClient(app)
    first = client.get("/api/jobs").json()["revision"]
    res = client.post("/api/node-action", json={"graph": g, "node": "j", "action": "Run"}).json()
    assert res["notes"] == ["Api-Job: queued"] and res["revision"] > first
    s = JobSpec("Api-Job", str(tmp_path))
    wait_for(jobs.runner, s, ("Completed",))
    client.get("/api/jobs")
    results = client.post("/api/run", json=g).json()["results"]
    assert results["j"]["display"]["title"] == "Job Api-Job: Completed"
    assert results["o"]["status"] == "ok", results["o"]  # the run's ODB opens downstream
    bad = client.post("/api/node-action", json={"graph": g, "node": "j", "action": "Explode"})
    assert bad.status_code == 400


def test_results_of_a_job_wait_instead_of_failing(tmp_path):
    from nodecode_fea.engine import Engine
    g = graph([("m", "model.model", {}), ("j", "model.job", {"name": "Later", "directory": str(tmp_path)}),
               ("o", "odb.open", {}), ("u", "odb.field", {"variable": "U"}), ("f", "odb.field", {"variable": "U"})],
              [("m", "model", "j", "analysis"), ("j", "path", "o", "path"), ("o", "odb", "u", "odb"),
               ("j", "path", "f", "odb")])  # a path straight into an ODB input waits the same way
    res, _ = Engine().evaluate(g)
    assert res["o"]["status"] == "error"  # nothing written yet: the path really is wrong for now
    (tmp_path / "Later.inp").write_text("*HEADING\n")
    res, _ = Engine().evaluate(g)
    assert res["o"]["status"] == "waiting" and "Run job Later" in res["o"]["message"]
    assert res["u"]["status"] == "waiting" and res["u"]["message"] == "Waiting for Open ODB"
    assert res["f"]["status"] == "waiting" and "Run job Later" in res["f"]["message"]
    (tmp_path / "Later.lck").write_text("")
    res, _ = Engine().evaluate(g)
    assert res["o"]["status"] == "waiting" and "no results written yet" in res["o"]["message"]
    (tmp_path / "Later.odb").write_text("half written")
    res, _ = Engine().evaluate(g)
    assert res["o"]["status"] == "waiting" and "first results aren't readable" in res["o"]["message"]


def test_a_running_jobs_results_open_and_follow_its_increments(tmp_path):
    """Open ODB reads what a running job has written so far; each increment (a new .sta line) re-reads it, and the
    /api/jobs revisions move so the editor and CAE show the new frames."""
    from nodecode_fea.engine import Engine
    g = graph([("m", "model.model", {}), ("j", "model.job", {"name": "Live", "directory": str(tmp_path)}),
               ("o", "odb.open", {})],
              [("m", "model", "j", "analysis"), ("j", "path", "o", "path")])
    (tmp_path / "Live.inp").write_text("*HEADING\n")
    (tmp_path / "Live.lck").write_text("")
    (tmp_path / "Live.sta").write_text(STA_HEADER + "   1     1   1     0     2     2  0.100      0.100      0.1000\n")
    (tmp_path / "Live.odb").write_text("mock://cantilever?nx=2&ny=1&nz=1&frames=2")
    client = TestClient(app)
    res = client.post("/api/run", json=g).json()["results"]
    assert res["o"]["status"] == "ok"
    items = dict(res["o"]["display"]["items"])
    assert items["Job"].startswith("Running: results so far") and "increment 1" in items["Job"]
    assert "(1 frames)" in items["Steps"] or "(2 frames)" in items["Steps"]
    before = client.get("/api/jobs").json()
    # the solver finishes another increment and writes another frame
    later = time.time() + 5
    with open(tmp_path / "Live.sta", "a") as fh:
        fh.write("   1     2   1     0     3     3  0.300      0.300      0.2000\n")
    (tmp_path / "Live.odb").write_text("mock://cantilever?nx=2&ny=1&nz=1&frames=3")
    os.utime(tmp_path / "Live.sta", (later, later))
    after = client.get("/api/jobs").json()
    assert after["revision"] > before["revision"] and after["results"] > before["results"]
    res, _ = Engine().evaluate(g)
    assert "(3 frames)" in dict(res["o"]["display"]["items"])["Steps"]
    os.utime(tmp_path / "Live.odb", (later + 1, later + 1))  # the frame is written after the .sta line
    typed = graph([("o", "odb.open", {"path": str(tmp_path / "Live.odb")})], [])  # no Job node upstream
    engine = Engine()
    assert "Job" in dict(engine.evaluate(typed)[0]["o"]["display"]["items"])
    os.remove(tmp_path / "Live.lck")  # the job ends: one more update, then the ODB isn't followed any more
    ended = client.get("/api/jobs").json()
    assert ended["results"] > after["results"]
    assert client.get("/api/jobs").json()["results"] == ended["results"]
    res, _ = engine.evaluate(typed)  # the same engine (its cache): the ODB is read again, now finished
    assert "Job" not in dict(res["o"]["display"]["items"])


STA_HEADER = """SUMMARY OF JOB INFORMATION:
 STEP  INC ATT SEVERE EQUIL TOTAL  TOTAL      STEP       INC OF       DOF    IF
               DISCON ITERS ITERS  TIME/      TIME/LPF    TIME/LPF    MONITOR RIKS
               ITERS               FREQ
"""


def test_status_file_increments_and_cutbacks():
    sta = STA_HEADER + """   1     1   1     0     1     1  0.100      0.100      0.1000
   1     2   1U    2     6     8  0.100      0.100      0.2000
   1     2   2     0     3     3  0.150      0.150      0.05000
   2     1   1     0     2     2   1.15       1.00      1.000
 THE ANALYSIS HAS COMPLETED SUCCESSFULLY
"""
    rows = jobs.parse_sta(sta)
    assert [(r["step"], r["increment"], r["attempts"]) for r in rows] == [(1, 1, 1), (1, 2, 2), (2, 1, 1)]
    assert rows[1]["total time"] == 0.15 and rows[1]["time increment"] == 0.05 and rows[2]["iterations"] == 2
    assert jobs.progress(sta) == "Step 2, increment 1, total time 1.15"
    explicit = """ SOLUTION PROGRESS

 STEP 1  ORIGIN 0.0000

  Total memory used for step 1 is approximately 5.1 megabytes.
  INCREMENT     STEP      TOTAL       CPU      STABLE    CRITICAL    KINETIC      TOTAL    PERCENT
                TIME       TIME      TIME   INCREMENT     ELEMENT     ENERGY     ENERGY  CHNG MASS
         0  0.000E+00  0.000E+00  00:00:00  1.000E-06        12  0.000E+00  0.000E+00  0.000E+00
       100  1.000E-04  1.000E-04  00:00:01  1.000E-06        12  2.000E+01  0.000E+00  0.000E+00

 STEP 2  ORIGIN 1.0000E-03

       200  5.000E-05  1.050E-03  00:00:02  5.000E-07        12  2.000E+01  0.000E+00  0.000E+00
"""
    rows = jobs.parse_sta(explicit)
    assert [(r["step"], r["increment"], r["total time"], r["time increment"]) for r in rows] == [
        (1, 0, 0.0, 1e-6), (1, 100, 1e-4, 1e-6), (2, 200, 1.05e-3, 5e-7)]
    assert rows[2]["step time"] == 5e-5


def test_job_shows_its_files_and_increments_as_it_runs(tmp_path):
    from nodecode_fea.engine import Engine
    g = graph([("m", "model.model", {}),
               ("j", "model.job", {"name": "Watch", "directory": str(tmp_path), "show": "Messages (.msg)"})],
              [("m", "model", "j", "analysis")])
    res, outs = Engine().evaluate(g)
    assert res["j"]["display"]["kind"] == "log" and "No Watch.msg yet" in res["j"]["display"]["text"]
    (tmp_path / "Watch.msg").write_text(" ***WARNING: 12 nodes are overconstrained\n")
    (tmp_path / "Watch.sta").write_text(STA_HEADER + "   1     1   1     0     2     2  0.250      0.250      0.2500\n"
                                        "   1     2   1     0     2     2  0.500      0.500      0.2500\n")
    res, outs = Engine().evaluate(g)
    d = res["j"]["display"]
    assert d["title"].startswith("Watch.msg") and "overconstrained" in d["text"] and d["path"].endswith("Watch.msg")
    assert "overconstrained" in outs.outputs["j"]["text"].value
    total, inc = outs.outputs["j"]["increments"]
    assert list(total.x) == [1.0, 2.0] and list(total.y) == [0.25, 0.5] and list(inc.y) == [0.25, 0.25]
    g["nodes"][1]["params"]["show"] = "State"
    res, outs = Engine().evaluate(g)
    assert res["j"]["display"]["kind"] == "info" and "0.500" in outs.outputs["j"]["text"].value


def test_the_abaqus_launcher_is_a_full_path(tmp_path, monkeypatch):
    """subprocess on Windows doesn't add '.bat' to a bare 'abaqus': jobs need the path shutil.which finds."""
    from nodecode_fea.sources import abaqus as source
    launcher = tmp_path / ("abaqus.bat" if os.name == "nt" else "abaqus")
    launcher.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.delenv("ABAQUS_CMD", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert source.abaqus_command().lower() == str(launcher).lower()
    # nothing on PATH, nothing at SIMULIA's default place: the bare name (and the editor says it's missing)
    monkeypatch.setattr(source, "DEFAULT_LAUNCHERS", ())
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert source.abaqus_command() == "abaqus"


def test_the_sets_abaqus_makes_for_its_warnings(tmp_path):
    """Set with Warn*, Err* gives each problem set Abaqus wrote into the ODB; the Job names them from its .msg."""
    from nodecode_fea.engine import Engine
    g = graph([("o", "odb.open", {"path": "mock://cantilever?nx=4&ny=1&nz=1&warnings=1"}),
               ("s", "odb.set", {"name": "Warn*, Err*"})], [("o", "odb", "s", "odb")])
    res, outs = Engine().evaluate(g)
    sets = outs.outputs["s"]["set"]
    assert [(x.name, x.kind) for x in sets] == [("WarnElemDistorted", "element"), ("WarnNodeOverconstr", "node")]
    assert len(sets[1].labels) == 3
    g["nodes"][0]["params"]["path"] = "mock://cantilever?nx=4&ny=1&nz=1"
    res, _ = Engine().evaluate(g)
    assert "Abaqus made no warning or error sets" in res["s"]["message"]
    (tmp_path / "Bad.msg").write_text(" ***WARNING: 3 elements are distorted. The elements have been identified in "
                                      "element set WarnElemDistorted.\n")
    g = graph([("m", "model.model", {}), ("j", "model.job", {"name": "Bad", "directory": str(tmp_path)})],
              [("m", "model", "j", "analysis")])
    res, _ = Engine().evaluate(g)
    assert dict(res["j"]["display"]["items"])["Problem sets"].startswith("WarnElemDistorted")


def test_convergence_troubles_show_on_the_job(tmp_path):
    from nodecode_fea import checks
    from nodecode_fea.engine import Engine
    (tmp_path / "Hard.inp").write_text("*HEADING\n")
    (tmp_path / "Hard.sta").write_text(STA_HEADER + "".join(
        f"   1     {i}   1U    3     9    12  0.100      0.100      0.1000\n"
        f"   1     {i}   2     0     4     4  {0.1 + i * 1e-5:.6f}   {0.1 + i * 1e-5:.6f}  {1e-5 if i > 1 else 0.1}\n"
        for i in range(1, 5)))
    (tmp_path / "Hard.msg").write_text(
        "     LARGEST RESIDUAL FORCE     -1.234E+03  AT NODE        34  DOF  2 INSTANCE: PART-1-1\n"
        " ***ERROR: TOO MANY ATTEMPTS MADE FOR THIS INCREMENT\n"
        " ***WARNING: SOLVER PROBLEM. ZERO PIVOT WHEN PROCESSING NODE 12 INSTANCE PART-1-1 D.O.F. 1\n")
    (tmp_path / "Hard.log").write_text("Abaqus/Analysis exited with errors\n")
    diag = jobs.diagnose(JobSpec("Hard", str(tmp_path)))
    assert diag[0].startswith("4 cutbacks in 4 increments")
    assert any(d.startswith("The time increment fell to 1e-05") for d in diag)
    assert any(d.startswith("An increment did not converge") for d in diag)
    assert any(d.startswith("Zero pivot") for d in diag)
    assert diag[-1].startswith("The largest residual force when it stopped was -1.234E+03 at node 34 of PART-1-1")
    g = graph([("m", "model.model", {}), ("j", "model.job", {"name": "Hard", "directory": str(tmp_path)})],
              [("m", "model", "j", "analysis")])
    res, inputs = Engine().evaluate(g)
    warnings = checks.lint(g, res, inputs)["j"]
    assert sum(w.startswith("Convergence: ") for w in warnings) == len(diag)
    assert not jobs.diagnose(JobSpec("Nothing", str(tmp_path)))


def test_results_sends_a_finished_job_to_post_processing(tmp_path):
    g = graph([("m", "model.model", {}), ("j", "model.job", {"name": "Done", "directory": str(tmp_path)})],
              [("m", "model", "j", "analysis")])
    client = TestClient(app)
    res = client.post("/api/node-action", json={"graph": g, "node": "j", "action": "Results"})
    assert res.status_code == 400 and "Run the job first" in res.json()["detail"]
    (tmp_path / "Done.odb").write_text("mock://cantilever?nx=2&ny=1&nz=1")
    res = client.post("/api/node-action", json={"graph": g, "node": "j", "action": "Results"}).json()
    assert res["open"] == [str(tmp_path / "Done.odb")]
    assert res["notes"] == ["Results of Done in the Results view (Job → Open ODB)"] and "revision" in res
