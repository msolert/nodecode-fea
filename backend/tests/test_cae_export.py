"""Generate a CAE script from a graph, run it against a recording fake of Abaqus/CAE, check the calls."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nodecode_fea.cae_export import build_script
from nodecode_fea.engine import Engine
from nodecode_fea.sources.abaqus import Worker

from .test_engine import MOCK, graph

from .fake_paths import FAKE_DIR as FAKE
from .fake_paths import LAUNCHER
URL = "mock://cantilever?nx=8&ny=2&nz=2&frames=4"


@pytest.fixture(scope="module")
def odb_path(tmp_path_factory):
    odb = tmp_path_factory.mktemp("cae") / "beam.odb"
    odb.write_text(URL)
    mp = pytest.MonkeyPatch()
    mp.setenv("ABAQUS_CMD", str(LAUNCHER))
    Worker._instance = None
    yield str(odb)
    Worker.get().proc.kill()
    Worker._instance = None
    mp.undo()


def run_in_fake_cae(script, tmp_path: Path, runs: int = 1) -> tuple[list[dict], str]:
    """Run a generated script ``runs`` times in one fake CAE session (as Live/Sync does). ``script`` can also be a
    list of scripts: successive updates (or snippets standing for the user working in CAE). Each run starts with a
    {"call": "--run--"} entry in the log."""
    scripts = script if isinstance(script, list) else [script] * runs
    paths = []
    for i, text in enumerate(scripts):
        paths.append(tmp_path / f"nocode_cae_{i}.py")
        paths[-1].write_text(text, encoding="utf-8")
    driver = tmp_path / "driver.py"
    driver.write_text("import abaqus\n"
                      f"for path in {[str(p) for p in paths]!r}:\n"
                      "    abaqus.LOG.append({'call': '--run--', 'args': [], 'kwargs': {}})\n"
                      "    exec(compile(open(path, 'rb').read(), path, 'exec'), {'__name__': '__main__'})\n")
    log = tmp_path / "log.json"
    env = dict(os.environ, PYTHONPATH=str(FAKE), NOCODE_CAE_LOG=str(log))
    proc = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(log.read_text()), proc.stdout


def calls(log, name):
    return [c for c in log if c["call"].endswith(name)]


def test_contour_chart_values_in_cae(odb_path, tmp_path):
    g = graph(
        [("odb", "odb.open", {"path": odb_path}), ("s", "odb.field", {"variable": "S", "frame": 2}),
         ("u", "odb.field", {"variable": "U", "frame": 2}), ("m", "field.invariant", {"invariant": "Mises"}),
         ("root", "odb.set", {"name": "ROOT_ELEMENTS"}), ("f", "field.filter", {}),
         ("max", "field.reduce", {"op": "Max"}), ("c", "view.contour", {"scale": 5, "bands": 8}),
         ("p", "field.probe", {"variable": "U", "quantity": "U2", "labels": "1, 2"}),
         ("chart", "view.chart", {"title": "Tip deflection", "viewport": "New"}), ("v", "view.value", {"label": "Peak", "unit": "MPa"}),
         ("t", "view.table", {})],
        [("odb", "odb", "s", "odb"), ("odb", "odb", "u", "odb"), ("odb", "odb", "root", "odb"),
         ("s", "field", "m", "field"), ("m", "field", "f", "field"), ("root", "set", "f", "set"),
         ("f", "field", "max", "field"), ("f", "field", "c", "field"), ("u", "field", "c", "deform"),
         ("max", "value", "c", "markers"), ("odb", "odb", "p", "odb"), ("p", "series", "chart", "series"),
         ("max", "value", "v", "value"), ("s", "field", "t", "field")],
    )
    results, inputs = Engine().evaluate(g)
    out = build_script(g, results, inputs)
    assert len(out["contents"]) == 4 and not out["skipped"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)

    assert "failed" not in stdout and stdout.strip().endswith("done.")
    assert calls(log, "session.openOdb")[0]["kwargs"]["path"] == odb_path
    assert calls(log, "odbDisplay.setFrame")[0]["kwargs"] == {"step": 0, "frame": 2}
    primary = calls(log, "odbDisplay.setPrimaryVariable")[0]["kwargs"]
    assert primary == {"variableLabel": "S", "outputPosition": "INTEGRATION_POINT",
                       "refinement": ["INVARIANT", "Mises"]}
    assert calls(log, "odbDisplay.setDeformedVariable")[0]["args"] == ["U"]
    assert calls(log, "commonOptions.setValues")[0]["kwargs"]["uniformScaleFactor"] == 5.0
    assert calls(log, "contourOptions.setValues")[0]["kwargs"]["numIntervals"] == 8
    assert calls(log, "dgo.LeafFromElementSets")[0]["kwargs"] == {"elementSets": ["ROOT_ELEMENTS"]}
    assert "visibleDisplayGroups" in calls(log, "odbDisplay.setValues")[0]["kwargs"]
    note = calls(log, "mdb.Text")[0]["kwargs"]["text"]
    assert note.startswith("Max S Mises")

    xy = calls(log, "session.XYData")
    assert len(xy) == 2 and len(xy[0]["kwargs"]["data"]) == 4
    assert xy[0]["kwargs"]["legendLabel"] == "U2 @ BEAM-1.1"
    assert calls(log, "chart.setValues")[0]["kwargs"]["curvesToPlot"] == ["<session.Curve()>"] * 2
    assert "Peak: " in stdout and "MPa" in stdout
    assert "S (first 10 of" in stdout


def test_demo_model_and_errors_are_skipped(tmp_path):
    g = graph(
        [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
         ("m", "field.invariant", {}), ("c", "view.contour", {}), ("bad", "view.chart", {})],
        [("odb", "odb", "s", "odb"), ("s", "field", "m", "field"), ("m", "field", "c", "field")],
    )
    results, inputs = Engine().evaluate(g)
    out = build_script(g, results, inputs)
    assert out["skipped"] == ["Chart 1 (not ready: Connect Series)"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "built-in demo model" in stdout and not calls(log, "session.openOdb")


def test_nothing_to_export():
    g = graph([("odb", "odb.open", {"path": MOCK})], [])
    results, inputs = Engine().evaluate(g)
    with pytest.raises(ValueError, match="Nothing to export"):
        build_script(g, results, inputs)


def test_first_output_reuses_viewport_1_and_reruns_reuse_viewports(odb_path, tmp_path):
    """CAE 2026 can crash showing an ODB in a new viewport: the first output goes to 'Viewport: 1'.

    Running the same script again (Live/Sync) must reuse viewports, never add more.
    """
    g = graph(
        [("odb", "odb.open", {"path": odb_path}), ("s", "odb.field", {"variable": "S"}),
         ("m", "field.invariant", {"invariant": "Mises"}), ("u", "odb.field", {"variable": "U"}),
         ("c", "field.component", {"component": "U2"}),
         ("c1", "view.contour", {"viewport": "New"}), ("c2", "view.contour", {"viewport": "New"}),
         ("h", "odb.history", {"region": "Node set FIXED", "output": "RF2"}),
         ("chart", "view.chart", {"title": "Reaction", "viewport": "New"})],
        [("odb", "odb", "s", "odb"), ("s", "field", "m", "field"), ("odb", "odb", "u", "odb"),
         ("u", "field", "c", "field"), ("m", "field", "c1", "field"), ("c", "field", "c2", "field"),
         ("odb", "odb", "h", "odb"), ("h", "series", "chart", "series")],
    )
    results, inputs = Engine().evaluate(g)
    out = build_script(g, results, inputs)
    log, stdout = run_in_fake_cae(out["script"], tmp_path, runs=2)
    assert "failed" not in stdout and stdout.count("done.") == 2

    # Run 1: contour 1 -> 'Viewport: 1'; contour 2 and the chart get new viewports. Run 2: no new ones.
    created = [c["kwargs"]["name"] for c in calls(log, "session.Viewport")]
    assert len(created) == 2 and "Viewport: 1" not in created
    shown = [c["call"].split("].")[0] + "]" for c in log if c["call"].endswith(".setValues")
             and "displayedObject" in c["kwargs"]]
    assert shown[0] == "viewport[Viewport: 1]"
    assert shown[:3] == shown[3:]  # the second run draws into exactly the same viewports


def test_a_running_jobs_odb_is_updated_in_cae(odb_path, tmp_path):
    """Each sync of a running job's results makes CAE read the frames written since (Odb.update); a finished
    job's ODB is just reused."""
    live = tmp_path / "Live.odb"
    live.write_text(URL)
    (tmp_path / "Live.lck").write_text("")
    g = graph([("odb", "odb.open", {"path": str(live)}), ("u", "odb.field", {"variable": "U"}),
               ("c", "view.contour", {})],
              [("odb", "odb", "u", "odb"), ("u", "field", "c", "field")])
    results, inputs = Engine().evaluate(g)
    assert results["odb"]["status"] == "ok"
    log, stdout = run_in_fake_cae(build_script(g, results, inputs)["script"], tmp_path, runs=2)
    assert "failed" not in stdout and len(calls(log, "session.openOdb")) == 1
    assert len(calls(log, "].update")) == 1  # the second sync reads the new frames
    os.remove(tmp_path / "Live.lck")
    log, _ = run_in_fake_cae(build_script(g, results, inputs)["script"], tmp_path, runs=2)
    assert not calls(log, "].update")


def _contours_and_chart(odb_path, modes, focus=None):
    g = graph(
        [("odb", "odb.open", {"path": odb_path}), ("s", "odb.field", {"variable": "S"}),
         ("m", "field.invariant", {"invariant": "Mises"}),
         ("c1", "view.contour", {"viewport": modes[0]}), ("c2", "view.contour", {"viewport": modes[1]}),
         ("h", "odb.history", {"region": "Node set FIXED", "output": "RF2"}),
         ("chart", "view.chart", {"title": "Reaction", "viewport": modes[2]})],
        [("odb", "odb", "s", "odb"), ("s", "field", "m", "field"), ("m", "field", "c1", "field"),
         ("m", "field", "c2", "field"), ("odb", "odb", "h", "odb"), ("h", "series", "chart", "series")],
    )
    g["focus"] = focus
    results, inputs = Engine().evaluate(g)
    return build_script(g, results, inputs)


def test_viewport_current_is_the_default(odb_path, tmp_path):
    out = _contours_and_chart(odb_path, ["Current", "Current", "Current"])
    assert len(out["contents"]) == 1 and "the current viewport" in out["contents"][0]
    log, stdout = run_in_fake_cae(out["script"], tmp_path, runs=2)
    assert stdout.count("done.") == 2 and "failed" not in stdout
    assert not calls(log, "session.Viewport")
    shown = {c["call"].split("].")[0] for c in log if c["call"].endswith(".setValues") and "displayedObject" in c["kwargs"]}
    assert shown == {"viewport[Viewport: 1"}


def test_viewport_new_next_to_current(odb_path, tmp_path):
    """A New output must not take over 'Viewport: 1' when Current outputs draw there."""
    out = _contours_and_chart(odb_path, ["Current", "New", "Current"])
    assert "its own viewport 'NoCode_Contour_c2'" in out["contents"][1]
    log, stdout = run_in_fake_cae(out["script"], tmp_path, runs=3)
    assert stdout.count("done.") == 3
    assert [c["kwargs"]["name"] for c in calls(log, "session.Viewport")] == ["NoCode_Contour_c2"]
    # After each run the current viewport is 'Viewport: 1' again, so Current outputs stay put.
    current = [c["call"] for c in log if c["call"].endswith(".makeCurrent")]
    assert current[-1] == "viewport[Viewport: 1].makeCurrent"


def test_cae_refinement_follows_the_variable(odb_path, tmp_path):
    """Strain gets a principal (never Mises); a scalar component (PEEQ) gets no refinement at all."""
    g = graph(
        [("odb", "odb.open", {"path": odb_path}), ("e", "odb.field", {"variable": "E"}),
         ("p", "field.invariant", {"invariant": "Max. Principal"}), ("c1", "view.contour", {}),
         ("q", "odb.field", {"variable": "PEEQ"}), ("pc", "field.component", {"component": "PEEQ"}),
         ("c2", "view.contour", {"viewport": "New"})],
        [("odb", "odb", "e", "odb"), ("e", "field", "p", "field"), ("p", "field", "c1", "field"),
         ("odb", "odb", "q", "odb"), ("q", "field", "pc", "field"), ("pc", "field", "c2", "field")],
    )
    results, inputs = Engine().evaluate(g)
    out = build_script(g, results, inputs)
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout
    primaries = [c["kwargs"] for c in calls(log, "odbDisplay.setPrimaryVariable")]
    assert primaries[0] == {"variableLabel": "E", "outputPosition": "INTEGRATION_POINT",
                            "refinement": ["INVARIANT", "Max. Principal"]}
    assert primaries[1] == {"variableLabel": "PEEQ", "outputPosition": "INTEGRATION_POINT"}


@pytest.mark.parametrize("focus, drawn", [(None, "Contour 2"), ("chart", "Reaction"), ("c1", "Contour 1"),
                                          ("nope", "Contour 2")])
def test_one_output_owns_the_current_viewport(odb_path, focus, drawn):
    """Outputs sharing the current viewport would overwrite each other: only the focused one (the one clicked
    last in the web app) is drawn there, else the last contour, so a chart never covers the contours."""
    out = _contours_and_chart(odb_path, ["Current", "Current", "Current"], focus)
    assert len(out["contents"]) == 1 and out["contents"][0].startswith(drawn)
    assert len(out["skipped"]) == 2 and all("shows another output" in s for s in out["skipped"])


def test_node_names_title_cae_outputs(odb_path):
    g = graph(
        [("odb", "odb.open", {"path": odb_path}), ("s", "odb.field", {"variable": "S"}),
         ("m", "field.invariant", {"invariant": "Mises"}), ("c", "view.contour", {}),
         ("max", "field.reduce", {}), ("v", "view.value", {})],
        [("odb", "odb", "s", "odb"), ("s", "field", "m", "field"), ("m", "field", "c", "field"),
         ("m", "field", "max", "field"), ("max", "value", "v", "value")],
    )
    g["nodes"][3]["name"] = "Von Mises at the end"
    g["nodes"][5]["name"] = "Peak stress"
    results, inputs = Engine().evaluate(g)
    out = build_script(g, results, inputs)
    assert out["contents"][0].startswith("Von Mises at the end in the current viewport")
    assert "Peak stress: " in out["script"]


def test_contour_results_are_marked_in_cae(odb_path, tmp_path):
    """Located values get a labelled arrow at their 3D position; fields/sets on Results become display groups."""
    g = graph(
        [("odb", "odb.open", {"path": odb_path}), ("s", "odb.field", {"variable": "S"}),
         ("m", "field.quantity", {"quantity": "Mises"}), ("max", "math.reduce", {"op": "Max"}),
         ("hot", "logic.test", {"op": ">", "value": "100"}), ("c", "view.contour", {})],
        [("odb", "odb", "s", "odb"), ("s", "field", "m", "field"), ("m", "field", "max", "field"),
         ("m", "field", "hot", "a"), ("m", "field", "c", "field"), ("max", "value", "c", "markers"),
         ("hot", "kept", "c", "markers")],
    )
    results, inputs = Engine().evaluate(g)
    out = build_script(g, results, inputs)
    assert "1 display group(s)" in out["contents"][0], out["contents"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path, runs=2)
    assert "failed" not in stdout and "could not" not in stdout, stdout
    arrows = calls(log, "mdb.Arrow")
    assert arrows and len(arrows[0]["kwargs"]["startAnchor"]) == 3
    labels = [c["kwargs"]["text"] for c in calls(log, "mdb.Text") if "anchor" in c["kwargs"]
              and isinstance(c["kwargs"]["anchor"], list)]
    assert any(t.startswith("Max S Mises") for t in labels)
    assert calls(log, "dgo.LeafFromElementLabels")[0]["kwargs"]["partInstanceName"] == "BEAM-1"
    assert calls(log, "session.DisplayGroup")


def test_odb_alone_shows_the_undeformed_model(odb_path, tmp_path):
    """Open ODB with nothing after it yet: CAE shows that ODB's undeformed mesh in the current viewport."""
    g = graph([("odb", "odb.open", {"path": odb_path})], [])
    results, inputs = Engine().evaluate(g)
    out = build_script(g, results, inputs)
    assert out["contents"][0].startswith("Model: beam.odb")
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout
    assert calls(log, "session.openOdb")[0]["kwargs"]["path"] == odb_path
    assert any(c["kwargs"].get("plotState") == ["UNDEFORMED"] for c in calls(log, "display.setValues"))


def test_model_only_when_no_contour_or_chart(odb_path):
    """A Contour or Chart takes over (New ones still reuse 'Viewport: 1' first, for CAE 2026); values alone don't."""
    g = graph(
        [("odb", "odb.open", {"path": odb_path}), ("s", "odb.field", {"variable": "S", "quantity": "Mises"}),
         ("r", "math.reduce", {}), ("v", "view.value", {})],
        [("odb", "odb", "s", "odb"), ("s", "field", "r", "field"), ("r", "value", "v", "value")],
    )
    results, inputs = Engine().evaluate(g)
    out = build_script(g, results, inputs)
    assert out["contents"][0].startswith("Model:") and len(out["contents"]) == 2
    g["nodes"].append({"id": "c", "type": "view.contour", "params": {"viewport": "New"}})
    g["edges"].append({"source": "s", "sourceHandle": "field", "target": "c", "targetHandle": "field"})
    results, inputs = Engine().evaluate(g)
    assert not any(c.startswith("Model:") for c in build_script(g, results, inputs)["contents"])


def test_field_only_on_a_display_group_in_cae(odb_path, tmp_path):
    """Display Group -> Field Output (Where) -> Contour: CAE shows the field only on the group's elements."""
    g = graph(
        [("odb", "odb.open", {"path": odb_path}), ("s", "odb.field", {"variable": "S", "quantity": "Mises"}),
         ("hot", "logic.test", {"op": ">", "value": "100"}), ("grp", "view.group", {"name": "Hot"}),
         ("u", "odb.field", {"variable": "U", "quantity": "Magnitude"}), ("c", "view.contour", {})],
        [("odb", "odb", "s", "odb"), ("s", "field", "hot", "a"), ("hot", "kept", "grp", "members"),
         ("odb", "odb", "u", "odb"), ("grp", "set", "u", "where"), ("u", "field", "c", "field")],
    )
    results, inputs = Engine().evaluate(g)
    assert results["u"]["status"] == "ok", results["u"]
    n_hot = len(inputs["grp"]["members"][0].labels) if isinstance(inputs["grp"]["members"], list) else 0
    assert n_hot and "nodes" in results["u"]["summary"]  # element group -> the nodes of those elements
    out = build_script(g, results, inputs)
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    leaves = calls(log, "dgo.LeafFromNodeLabels")
    assert leaves and leaves[0]["kwargs"]["partInstanceName"] == "BEAM-1"
    assert any("visibleDisplayGroups" in c["kwargs"] for c in calls(log, "odbDisplay.setValues"))


def test_an_odb_from_an_older_release_is_shown_upgraded(odb_path, tmp_path):
    """Solved with 2025, shown in CAE 2026: CAE opens an upgraded copy (once per session, under the file's own name);
    the file itself is left as it is."""
    old = tmp_path / "old-beam.odb"
    shutil.copyfile(odb_path, old)
    g = graph([("odb", "odb.open", {"path": str(old)}), ("s", "odb.field", {"variable": "S", "quantity": "Mises"}),
               ("c", "view.contour", {})], [("odb", "odb", "s", "odb"), ("s", "field", "c", "field")])
    results, inputs = Engine().evaluate(g)
    assert results["c"]["status"] == "ok", results["c"]
    log, stdout = run_in_fake_cae(build_script(g, results, inputs)["script"], tmp_path, runs=2)
    assert "failed" not in stdout and "older Abaqus release" in stdout, stdout
    up = calls(log, "odbAccess.upgradeOdb")
    assert len(up) == 1 and up[0]["kwargs"]["existingOdbPath"] == str(old)
    opened = calls(log, "session.openOdb")[0]["kwargs"]
    assert opened == {"name": str(old), "path": up[0]["kwargs"]["upgradedOdbPath"]}
    assert old.read_text() == Path(odb_path).read_text()


def test_annotations_go_to_mdb_like_cae(odb_path, tmp_path):
    """CAE has no session.annotations: notes and arrows must use mdb (this failed on CAE 2026)."""
    g = graph(
        [("odb", "odb.open", {"path": odb_path}), ("u", "odb.field", {"variable": "U", "quantity": "Magnitude"}),
         ("mx", "math.reduce", {}), ("c", "view.contour", {})],
        [("odb", "odb", "u", "odb"), ("u", "field", "mx", "field"), ("u", "field", "c", "field"),
         ("mx", "value", "c", "markers")],
    )
    results, inputs = Engine().evaluate(g)
    log, stdout = run_in_fake_cae(build_script(g, results, inputs)["script"], tmp_path, runs=2)
    assert "failed" not in stdout and "could not" not in stdout, stdout
    assert calls(log, "mdb.Text") and calls(log, "mdb.Arrow")


def test_contour_options_and_markers_on_the_deformed_shape(odb_path, tmp_path):
    g = graph(
        [("odb", "odb.open", {"path": odb_path}), ("s", "odb.field", {"variable": "S", "quantity": "Mises"}),
         ("u", "odb.field", {"variable": "U"}), ("mx", "math.reduce", {}),
         ("c", "view.contour", {"min": "0", "max": "300", "spectrum": "Reversed rainbow", "minmax": "On",
                                "edges": "Feature edges", "view": "Front", "bands": 6})],
        [("odb", "odb", "s", "odb"), ("odb", "odb", "u", "odb"), ("s", "field", "mx", "field"),
         ("s", "field", "c", "field"), ("u", "field", "c", "deform"), ("mx", "value", "c", "markers")],
    )
    results, inputs = Engine().evaluate(g)
    shown = results["c"]["display"]
    assert shown["min"] == 0 and shown["max"] == 300 and shown["deformScale"] > 0
    assert sum(1 for m in shown["markers"] if m.get("extreme")) == 2
    log, stdout = run_in_fake_cae(build_script(g, results, inputs)["script"], tmp_path)
    assert "failed" not in stdout and "not accepted" not in stdout, stdout
    opts = calls(log, "contourOptions.setValues")[0]["kwargs"]
    assert opts["spectrum"] == "Reversed rainbow" and opts["minValue"] == 0 and opts["maxValue"] == 300
    assert opts["showMaxLocation"] == "ON" and opts["numIntervals"] == 6
    common = [c["kwargs"] for c in calls(log, "commonOptions.setValues")]
    assert any(k.get("uniformScaleFactor") == pytest.approx(shown["deformScale"], rel=1e-3) for k in common)
    assert any(k.get("visibleEdges") == "FEATURE" for k in common)
    assert calls(log, "view.setValues")[0]["args"] == ["<views[Front]>"]
    # the Reduce arrow points at the deformed position the editor shows (not the undeformed one)
    peak = next(m for m in shown["markers"] if not m.get("extreme"))
    anchor = calls(log, "mdb.Arrow")[0]["kwargs"]["startAnchor"]
    assert anchor == pytest.approx(list(peak["position"]), abs=1e-5)


@pytest.mark.parametrize("nodes, edges, label", [
    ([("s", "odb.field", {"variable": "S", "quantity": "Mises", "combine": "Max over frames"})], [], "S-Mises-max"),
    ([("s", "odb.field", {"variable": "S", "quantity": "Mises"}), ("fx", "math.formula", {"expr": "a / 355"})],
     [("s", "field", "fx", "a")], "f-S-Mises"),
])
def test_computed_fields_go_to_cae_as_session_fields(odb_path, tmp_path, nodes, edges, label):
    """Formula results and combined frames are shown in CAE from the editor's own values (a session field)."""
    src = nodes[-1][0]
    port = "value" if src == "fx" else "field"
    g = graph([("odb", "odb.open", {"path": odb_path})] + nodes + [("c", "view.contour", {})],
              [("odb", "odb", "s", "odb")] + edges + [(src, port, "c", "field")])
    results, inputs = Engine().evaluate(g)
    assert results["c"]["status"] == "ok", results["c"]
    out = build_script(g, results, inputs)
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    fo = calls(log, ".FieldOutput")[0]["kwargs"]
    assert fo["name"].startswith(label) and fo["type"] == "SCALAR"
    data = calls(log, ".addData")
    n_rows = sum(len(c["kwargs"]["labels"]) for c in data)
    assert n_rows == len(inputs["c"]["field"].labels)
    assert calls(log, "odbDisplay.setFrame")[0]["kwargs"] == {"step": "Session Step", "frame": 0}
    shown = calls(log, "odbDisplay.setPrimaryVariable")[0]["kwargs"]
    assert shown["variableLabel"] != "S"
    assert shown["outputPosition"] == "ELEMENT_CENTROID"  # real CAE refuses CENTROID for centroid session data
