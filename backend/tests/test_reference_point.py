"""A reference point made on a part (Part Feature), carried by its instances, taking loads, BCs, couplings,
connectors and history output."""

from .test_cae_export import calls, run_in_fake_cae
from .test_model import evaluate, script

BASE = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
        ("rp", "model.feature", {"operation": "Reference point", "name": "Top", "point": "50, 25, 30"}),
        ("i", "model.instance", {})]
BASE_EDGES = [("s", "sketch", "p", "profile"), ("p", "part", "rp", "part"), ("rp", "part", "i", "part")]


def test_the_part_and_the_point_show_where_it_is():
    _, res, _ = evaluate(BASE, BASE_EDGES)
    assert res["rp"]["status"] == "ok", res["rp"]
    d = res["rp"]["display"]
    assert d["kind"] == "model3d" and d["datums"] == [{"name": "Top", "kind": "point", "origin": [50.0, 25.0, 30.0]}]
    shown = res["rp"]  # the point output on its own (a Region) shows the same marker
    assert shown["status"] == "ok"


def test_loads_bcs_and_history_on_it_reach_every_instance(tmp_path):
    nodes = BASE + [("j", "model.instance", {"name": "B", "x": 200}),
                    ("step", "model.step", {"name": "Pull", "history": "RF1, U1"}),
                    ("hold", "model.condition", {"name": "Hold", "kind": "Fixed"}),
                    ("pull", "model.condition", {"name": "Pull-force", "kind": "Concentrated force", "x": 100})]
    edges = BASE_EDGES + [("rp", "part", "j", "part"), ("step", "step", "hold", "step"),
                          ("rp", "point", "hold", "region"), ("hold", "condition", "pull", "step"),
                          ("rp", "point", "pull", "region"), ("rp", "point", "step", "history_region")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    code = out["script"]
    assert "part.ReferencePoint(point=(50.0, 25.0, 30.0))" in code
    assert "_rp_pick(mdb.models['NoCode-Model'].parts['Part-1'], 'Top')" in code
    log, stdout = run_in_fake_cae(code, tmp_path)
    assert "failed" not in stdout and "Traceback" not in stdout, stdout
    assert calls(log, ".ConcentratedForce")
    regions = [str(c["kwargs"]) for c in calls(log, "regionToolset.Region")]
    on_rp = [r for r in regions if "referencePoints" in r]
    assert on_rp and all("instances[B]" in r and "instances[Part-1-1]" in r for r in on_rp), regions
    assert calls(log, ".EncastreBC") and calls(log, ".HistoryOutputRequest")


def test_it_is_a_couplings_control_point_and_a_connector_end(tmp_path):
    nodes = BASE + [("face", "model.region", {"kind": "Face", "value": "0"}),
                    ("step", "model.step", {}),
                    ("rbe", "model.interaction", {"name": "Grip", "kind": "Kinematic coupling"}),
                    ("wire", "model.interaction", {"name": "Link", "kind": "Connector", "point2": "0, 0, 500"})]
    edges = BASE_EDGES + [("i", "instance", "face", "source"), ("face", "region", "rbe", "slave"),
                          ("rp", "point", "rbe", "master"), ("step", "step", "rbe", "step"),
                          ("rp", "point", "wire", "master"), ("step", "step", "wire", "step")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout and "Traceback" not in stdout, stdout
    assert calls(log, ".Coupling") and calls(log, ".WirePolyLine")
    (grip,) = [c for c in calls(log, ".Set") if c["kwargs"].get("name") == "Grip-RP"]
    assert "referencePoints" in grip["kwargs"]


def test_what_a_reference_point_cannot_be():
    _, res, _ = evaluate(BASE + [("bad", "model.feature", {"operation": "Reference point", "point": "1, 2, 3; 4, 5, 6"}),
                                 ("mesh", "model.mesh", {}), ("m", "model.measure", {"quantity": "Area"})],
                         BASE_EDGES + [("p", "part", "bad", "part"), ("rp", "point", "mesh", "target"), ("rp", "point", "m", "first")])
    assert "one x, y, z" in res["bad"]["message"]
    assert res["mesh"]["status"] == "error" and res["m"]["status"] == "error"
