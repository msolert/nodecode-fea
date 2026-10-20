"""The live CAE link, end to end against a simulated CAE session (tests/fake_abaqus/fake_cae.py).

The real link files (plug-in + kernel module) are loaded into the simulation, so this
checks the launch, the inbox/outbox protocol, heartbeats, script execution and picking.
"""

import time

import pytest

from nodecode_fea.cae_export import build_script
from nodecode_fea.cae_link import CaeLink, LinkError
from nodecode_fea.engine import Engine

from .test_engine import MOCK, graph, run
from .test_worker import FAKE


@pytest.fixture
def link(tmp_path, monkeypatch):
    monkeypatch.setenv("ABAQUS_CMD", str(FAKE))
    monkeypatch.setenv("NODECODE_FEA_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_PICK", "BEAM-1:5,BEAM-1:9")
    lk = CaeLink()
    yield lk
    lk.stop()


def wait_connected(lk, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if lk.status()["state"] == "connected":
            return
        time.sleep(0.2)
    raise AssertionError(f"never connected: {lk.status()}")


def chart_script():
    g = graph([("odb", "odb.open", {"path": MOCK}), ("u", "odb.history", {"region": "Node set FIXED", "output": "RF2"}),
               ("c", "view.chart", {"title": "Reaction"})],
              [("odb", "odb", "u", "odb"), ("u", "series", "c", "series")])
    results, inputs = Engine().evaluate(g)
    return build_script(g, results, inputs)["script"]


def test_launch_sync_and_pick(link):
    assert link.status()["state"] == "off"
    with pytest.raises(LinkError, match="Launch CAE"):
        link.send("ping")

    st = link.launch(chart_script())
    assert st["state"] in ("starting", "connected")
    wait_connected(link)
    status = link.status()
    assert status["kernelReady"]
    assert any("link started" in line for line in status["log"])

    initial = link.wait("initial", 10)
    assert initial["ok"] and "done." in initial["output"]

    rid = link.send("exec", script=chart_script())
    res = link.wait(rid, 10)
    assert res["ok"], res
    assert "Reaction: 1 curve" not in res["output"]  # (that's the editor's summary, not script output)
    assert res["output"].strip().endswith("done.")

    bad = link.send("exec", script="raise RuntimeError('boom')")
    res = link.wait(bad, 10)
    assert not res["ok"] and "boom" in res["error"]

    pick = link.send("pick", kind="node")
    res = link.wait(pick, 10)
    assert res == {"ok": True, "kind": "node", "labels": ["BEAM-1.5", "BEAM-1.9"]}, link.status()["log"]
    log = "\n".join(link.status()["log"])
    assert "pick procedures ready" in log and "pick failed" not in log
    # a second pick reuses the procedure created at start-up
    res = link.wait(link.send("pick", kind="element"), 10)
    assert res["ok"] and res["kind"] == "element"
    # Modelling objects use the same click-and-Done interaction. The current context identifies which displayed
    # part owns the clicked geometry; the picked object is returned as a non-owning model reference.
    res = link.wait(link.send("pick", kind="part"), 10)
    assert res == {"ok": True, "kind": "part", "value": "Fake-Model|Complex-Part"}
    res = link.wait(link.send("pick", kind="face"), 10)
    assert res == {"ok": True, "kind": "face", "value": "@5,0,0;9,0,0"}
    res = link.wait(link.send("pick", kind="geometry"), 10)
    assert res == {"ok": True, "kind": "face", "value": "@5,0,0;9,0,0"}

    with pytest.raises(LinkError, match="already running"):
        link.launch()
    link.stop()
    assert link.status()["state"] == "closed"


def test_picked_in_cae_node():
    res = run(
        [("odb", "odb.open", {"path": MOCK}),
         ("p", "cae.pick", {"kind": "node", "labels": "BEAM-1.5, 9"}),
         ("none", "cae.pick", {"kind": "node", "labels": ""}),
         ("u", "odb.field", {"variable": "U"}), ("f", "field.filter", {})],
        [("odb", "odb", "p", "odb"), ("odb", "odb", "none", "odb"), ("odb", "odb", "u", "odb"),
         ("u", "field", "f", "field"), ("p", "set", "f", "set")],
    )
    assert res["p"]["summary"] == "2 nodes"
    assert res["f"]["display"]["total"] == 2
    assert res["none"]["status"] == "error" and "Pick in CAE" in res["none"]["message"]

    two = "mock://cantilever?instances=2&nx=8&ny=2&nz=2&frames=4"
    res = run([("odb", "odb.open", {"path": two}), ("p", "cae.pick", {"kind": "element", "labels": "3"})],
              [("odb", "odb", "p", "odb")])
    assert res["p"]["status"] == "error" and "BEAM-1, BEAM-2" in res["p"]["message"]


def test_status_endpoint_when_off(monkeypatch):
    from fastapi.testclient import TestClient

    from nodecode_fea import app as app_module
    monkeypatch.setattr(app_module, "link", CaeLink())
    client = TestClient(app_module.app)
    assert client.get("/api/cae/status").json() == {"state": "off"}
    r = client.post("/api/cae/pick", json={"kind": "node"})
    assert r.status_code == 400 and "Launch CAE" in r.json()["detail"]


def test_repeated_syncs_reuse_viewports(link, tmp_path):
    """Live/Sync runs the script again and again in the same CAE session: no new viewports after the first."""
    odb = tmp_path / "beam.odb"
    odb.write_text("mock://cantilever?nx=8&ny=2&nz=2&frames=4")
    g = graph([("odb", "odb.open", {"path": str(odb)}), ("s", "odb.field", {"variable": "S"}),
               ("m", "field.invariant", {}), ("c", "view.contour", {"viewport": "New"}),
               ("k", "view.contour", {"viewport": "New"})],
              [("odb", "odb", "s", "odb"), ("s", "field", "m", "field"), ("m", "field", "c", "field"),
               ("m", "field", "k", "field")])
    results, inputs = Engine().evaluate(g)
    script = build_script(g, results, inputs)["script"]
    link.launch()
    wait_connected(link)
    for _ in range(3):
        assert link.wait(link.send("exec", script=script), 10)["ok"]
    probe = link.send("exec", script="from abaqus import session\nprint(sorted(session.viewports.keys()))")
    out = link.wait(probe, 10)["output"]
    assert out.strip() == "['NoCode_Contour_k', 'Viewport: 1']"


def test_updates_wait_for_a_pick_and_a_closed_pick_is_reported(link, tmp_path, monkeypatch):
    """Switching module cancels a pick in CAE (even to the current module, which every update does first), so
    updates wait while the user picks; queries that stay in the module still run. A pick closed in CAE without
    Done is reported as cancelled instead of leaving the editor waiting."""
    done = tmp_path / "done"
    monkeypatch.setenv("FAKE_PICK_HOLD", str(done))
    link.launch()
    wait_connected(link)
    pick = link.send("pick", kind="face")
    update = link.send("exec", script="print('updated')", module="Part")
    query = link.send("exec", script="print('listed')", module="")
    assert link.wait(query, 10)["output"].strip() == "listed"
    assert link.wait(update, 1.5) is None and link.result(pick) is None
    done.write_text("")
    assert link.wait(pick, 10) == {"ok": True, "kind": "face", "value": "@5,0,0;9,0,0"}
    assert link.wait(update, 10)["output"].strip() == "updated"
    assert any("waits for the pick" in line for line in link.status()["log"])

    pick = link.send("pick", kind="edge")
    time.sleep(1.2)
    again = link.send("pick", kind="face")  # a second pick replaces the first
    res = link.wait(pick, 10)
    assert res["ok"] is False and res["cancelled"] and "new pick" in res["error"]
    link.send("exec", script="import abaqusGui\nabaqusGui.switchModule('Part')", module="")  # closed in CAE
    res = link.wait(again, 10)
    assert res["ok"] is False and res["cancelled"] and "closed in CAE" in res["error"]


def test_first_display_runs_in_the_visualization_module(link):
    """The graph's first display on launch runs after CAE has switched to Visualization."""
    probe = "import abaqusGui\nprint('module=' + abaqusGui.MODULE['current'])\n"
    link.launch(probe)
    wait_connected(link)
    initial = link.wait("initial", 10)
    assert initial["ok"] and "module=Visualization" in initial["output"], initial


def test_the_link_waits_for_caes_main_window(link, monkeypatch):
    """CAE 2026 runs plug-ins before its main window is up; switching module then leaves the Part module's menus
    under a Module box that says Visualization (and an empty viewport). The first display waits for the window."""
    monkeypatch.setenv("FAKE_HIDDEN_TICKS", "6")
    probe = "import abaqusGui\nprint('module=' + abaqusGui.MODULE['current'])\n"
    link.launch(probe)
    wait_connected(link)
    initial = link.wait("initial", 10)
    assert initial["ok"] and "module=Visualization\n" in initial["output"], initial


def test_modelling_display_selects_the_model_tree(link):
    """A modelling graph must show the Model tree, even if CAE previously had the Results tab selected."""
    probe = ("import abaqusGui\n"
             "print('module=' + abaqusGui.MODULE['current'])\n"
             "print('tree=' + abaqusGui.TREE_TAB['current'])\n")
    link.launch(probe, module="Part")
    wait_connected(link)
    initial = link.wait("initial", 10)
    assert initial["ok"] and "module=Part" in initial["output"] and "tree=Model" in initial["output"], initial


def test_a_shown_sketch_is_opened_from_the_sketch_modules_list(link):
    """CAE's Sketch module only shows a sketch opened from its Sketch list (a GUI procedure): the plug-in picks the
    model and the sketch there after the script built it, at launch and on each update."""
    nodes = [("m", "model.model", {"name": "Defeature"}), ("a", "model.sketch", {"name": "Flange"}),
             ("b", "model.sketch", {"name": "Bore", "shape": "Circle"})]
    edges = [("m", "model", "a", "model"), ("m", "model", "b", "model")]

    def script(focus):
        g = graph(nodes, edges)
        g["focus"] = focus
        results, inputs = Engine().evaluate(g)
        return build_script(g, results, inputs)

    first = script("a")
    assert first["module"] == "Sketch" and first["sketch"] == ("Defeature", "Flange")
    link.launch(first["script"], module="Sketch", sketch=first["sketch"])
    wait_connected(link)
    assert link.wait("initial", 10)["ok"]
    second = script("b")
    assert link.wait(link.send("exec", script=second["script"], module="Sketch", sketch=second["sketch"]), 10)["ok"]
    probe = "import abaqusGui\nprint('opened=%r' % abaqusGui.OPENED)\n"
    end = time.time() + 10
    while time.time() < end:
        out = link.wait(link.send("exec", script=probe, module=""), 10)["output"]
        if "'Bore'" in out:
            break
        time.sleep(0.3)
    assert "opened=[('Defeature', 'Flange'), ('Defeature', 'Bore')]" in out, (out, link.status()["log"])
    assert any("sketch Bore opened" in line for line in link.status()["log"])


def test_viewer_launch(link):
    """Abaqus/Viewer (results only) runs the same link: launched as 'abaqus viewer script=…'."""
    st = link.launch("print('hello from the viewer')\n", product="viewer")
    assert st["product"] == "viewer"
    wait_connected(link)
    initial = link.wait("initial", 10)
    assert initial["ok"] and "hello from the viewer" in initial["output"]


def test_auto_picks_viewer_for_post_processing(monkeypatch):
    from fastapi.testclient import TestClient

    from nodecode_fea import app as app_module
    seen = {}

    def fake_launch(script, product="cae", module="Visualization", sketch=None):
        seen["product"] = product
        return {"state": "starting", "product": product}

    monkeypatch.setattr(app_module.link, "launch", fake_launch)
    client = TestClient(app_module.app)
    graph = {"nodes": [{"id": "o", "type": "odb.open", "params": {"path": "mock://cantilever"}}], "edges": []}
    assert client.post("/api/cae/launch", json={"graph": graph}).json()["product"] == "viewer"
    assert client.post("/api/cae/launch", json={"graph": graph, "product": "cae"}).json()["product"] == "cae"


def test_log_shows_what_cae_reported_under_each_update(tmp_path):
    """The editor's Log lists each update with what CAE printed for it (and errors), not only the link's requests."""
    import json
    from nodecode_fea.cae_link import CaeLink
    link = CaeLink()
    link.session = tmp_path
    (tmp_path / "outbox").mkdir()
    (tmp_path / "link.log").write_text("09:00:00 gui: request r1-1 (exec)\n09:00:05 gui: request r1-2 (exec)\n"
                                       "09:00:09 gui: request r1-3 (exec)\n")
    (tmp_path / "outbox" / "r1-1.json").write_text(json.dumps({"ok": True, "output":
        "NodeCode FEA: instance M I failed: name 'inf' is not defined\nNodeCode FEA: model: 1 built\n"
        "__NODECODE_FEA_MESH__{\"nodes\":{\"449\":[365.5,-49.0,25.0]}}\n"
        "NodeCode FEA: done.\n"}))
    (tmp_path / "outbox" / "r1-2.json").write_text(json.dumps({"ok": False, "output": "",
                                                               "error": "Traceback...\nNameError: boom\n"}))
    log = link.log_tail()
    assert log == ["09:00:00 gui: request r1-1 (exec)", "    instance M I failed: name 'inf' is not defined",
                   "    model: 1 built", "09:00:05 gui: request r1-2 (exec)", "    ERROR: Traceback...",
                   "    ERROR: NameError: boom", "09:00:09 gui: request r1-3 (exec)", "    (no answer from CAE yet)"]


def test_kernel_hides_machine_payloads_from_cae_but_returns_them(tmp_path, capsys):
    """Large editor responses stay in the outbox without flooding CAE's message area."""
    import importlib.util
    import json
    from pathlib import Path

    path = Path(__file__).parents[1] / "nodecode_fea" / "cae_link_files" / "nocode_kernel.py"
    spec = importlib.util.spec_from_file_location("nocode_kernel_test", path)
    kernel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kernel)
    kernel.SESSION = str(tmp_path)
    kernel.OUTBOX = str(tmp_path / "outbox")
    kernel.LOG = str(tmp_path / "link.log")
    script = tmp_path / "response.py"
    script.write_text(
        "from __future__ import print_function\n"
        "print('NodeCode FEA: model built')\n"
        "print('__NODECODE_FEA_MESH__{\\\"nodes\\\":{\\\"449\\\":[365.5,-49.0,25.0]}}')\n"
        "print('__NODECODE_FEA_MEASURE__{\\\"value\\\":1.0}')\n"
        "print('__NODECODE_FEA_OBJECTS__[\\\"Model-1|Part-1\\\"]')\n",
        encoding="utf-8",
    )

    kernel.run(str(script), "r1")

    shown = capsys.readouterr().out
    assert "model built" in shown
    assert "__NODECODE_FEA_" not in shown
    result = json.loads((tmp_path / "outbox" / "r1.json").read_text())
    assert result["ok"]
    assert "__NODECODE_FEA_MESH__" in result["output"]
    assert "__NODECODE_FEA_MEASURE__" in result["output"]
    assert "__NODECODE_FEA_OBJECTS__" in result["output"]
