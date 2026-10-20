"""Modelling: Model -> Sketch -> Part -> Instance, built in the fake CAE and rebuilt only where something changed."""

import json
import math
import re

import pytest
from fastapi.testclient import TestClient

from nodecode_fea.cae_export import build_script
from nodecode_fea import cae_mesh, cae_queries
from nodecode_fea.engine import Engine
from nodecode_fea.values import Number, PendingMeasure, Region

from .test_cae_export import calls, odb_path, run_in_fake_cae  # noqa: F401 (odb_path: a fixture)
from .test_engine import graph


def evaluate(nodes, edges, **extra):
    g = graph(nodes, edges)
    g.update(extra)
    res, inputs = Engine().evaluate(g)
    return g, res, inputs


def script(nodes, edges, previous=None, **extra):
    """previous: the output of the last update (its fingerprints and what each node made), as the live link keeps."""
    g, res, inputs = evaluate(nodes, edges, **extra)
    bad = {k: v["message"] for k, v in res.items() if v["status"] != "ok"}
    if previous is not None and "fingerprints" not in previous:
        previous = {"fingerprints": previous, "node_keys": {}}
    out = build_script(g, res, inputs, previous=(previous or {}).get("fingerprints"),
                       previous_keys=(previous or {}).get("node_keys"))
    return out, bad


def runs(log):
    """The log split per run (script) in the fake CAE session."""
    out = []
    for c in log:
        if c["call"] == "--run--":
            out.append([])
        else:
            out[-1].append(c)
    return out


def chain(sketch=None, part=None, inst=None, model=None):
    """Model -> Sketch -> Part -> Instance."""
    return ([("m", "model.model", model or {}), ("s", "model.sketch", sketch or {}),
             ("p", "model.part", part or {}), ("i", "model.instance", inst or {})],
            [("m", "model", "p", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "i", "part")])


@pytest.mark.parametrize("sketch, part, command", [
    ({}, {}, "BaseShell"),                                                           # flat plate
    ({}, {"method": "Extrude", "depth": 30}, "BaseShellExtrude"),                    # box of shell walls
    ({"shape": "Circle"}, {"method": "Extrude", "depth": 200}, "BaseShellExtrude"),  # tube
    ({"shape": "Profile", "points": "0,0; 100,0; 100,50"}, {"body": "Wire"}, "BaseWire"),
    ({}, {"body": "Solid", "method": "Extrude", "depth": 20}, "BaseSolidExtrude"),
    ({"x": 50}, {"body": "Solid", "method": "Revolve", "angle": 90}, "BaseSolidRevolve"),
    ({"shape": "Profile", "points": "10,0; 20,0; 20,50"}, {"method": "Revolve"}, "BaseShellRevolve"),
    ({"shape": "Profile", "points": "0,0; 100,0; 50,80", "closed": "Closed"}, {}, "BaseShell"),
])
def test_parts_build_in_cae(sketch, part, command, tmp_path):
    out, bad = script(*chain(sketch, part))
    assert not bad, bad
    assert out["module"] == "Assembly"  # the instance (furthest downstream) is shown
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, command), [c["call"] for c in log]
    assert calls(log, "rootAssembly.Instance")
    assert len(calls(log, "mdb.Model")) == 1


def test_3d_points_make_a_wire_frame(tmp_path):
    out, bad = script([("pts", "input.value", {"type": "List / matrix",
                                               "value": "[[0, 0, 0], [0, 0, 300], [200, 0, 300]]"}),
                       ("p", "model.part", {"name": "Frame", "body": "Wire"})], [("pts", "value", "p", "profile")])
    assert not bad, bad
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert calls(log, "WirePolyLine") and "failed" not in stdout
    assert calls(log, "mdb.Model")[0]["kwargs"]["name"] == "NoCode-Model"  # no Model node: the default model
    assert not calls(log, "Instance")  # parts reach the assembly only through Instance


def test_existing_cae_sketch_and_part_are_referenced_not_rebuilt(tmp_path):
    nodes = [("s", "input.value", {"type": "Sketch in CAE", "value": "Hand Model|Complex Profile"}),
             ("p", "model.part", {"name": "From-Sketch"}),
             ("old", "input.value", {"type": "Part in CAE", "value": "Hand Model|Existing Part"}),
             ("i", "model.instance", {"name": "Existing-Part-2"})]
    edges = [("s", "value", "p", "profile"), ("old", "value", "i", "part")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "objectToCopy=m.sketches['Complex Profile']" in out["script"]
    assert "part=m.parts['Existing Part']" in out["script"]
    assert "name='Complex Profile'" not in out["script"]
    assert "name='Existing Part'" not in out["script"]


def test_existing_instance_passes_unchanged_but_is_not_transformed():
    base = [("picked", "input.value", {"type": "Instance in CAE", "value": "Hand Model|Beam-1|Beam"}),
            ("i", "model.instance", {})]
    edges = [("picked", "value", "i", "part")]
    _, res, inputs = evaluate(base, edges)
    assert res["i"]["status"] == "ok" and inputs.outputs["i"]["instance"].existing
    _, res, _ = evaluate([base[0], ("i", "model.instance", {"x": 10})], edges)
    assert res["i"]["status"] == "error" and "repeatedly" in res["i"]["message"]


def test_part_feature_partitions_lists_by_datum_plane():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
             ("offsets", "input.value", {"type": "List / matrix", "value": "10, 20, 30"}),
             ("f", "model.feature", {"plane": "YZ"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "f", "part"),
             ("offsets", "value", "f", "param:offset")]
    g, res, inputs = evaluate(nodes, edges)
    assert res["f"]["status"] == "ok"
    assert [(p.operations[-1].datum.plane, p.operations[-1].datum.offset)
            for p in inputs.outputs["f"]["part"]] == [("YZ", 10.0), ("YZ", 20.0), ("YZ", 30.0)]
    out = build_script(g, res, inputs)
    assert out["script"].count("PartitionCellByDatumPlane") == 3


def test_region_pick_find_at_and_restricted_partition():
    base = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
            ("r", "model.region", {"kind": "Face", "value": "0, 2"}),
            ("f", "model.feature", {"plane": "YZ", "offset": 10})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "r", "source"),
             ("p", "part", "f", "part"), ("r", "region", "f", "region")]
    g, res, inputs = evaluate(base, edges)
    assert res["f"]["status"] == "ok"
    region = inputs.outputs["r"]["region"]
    assert isinstance(region, Region) and region.indices == (0, 2)
    out = build_script(g, res, inputs)
    assert "_picked = tuple(part.faces[i] for i in (0, 2))" in out["script"]
    assert "PartitionFaceByDatumPlane(faces=targets" in out["script"]

    points = [("pts", "input.value", {"type": "List / matrix", "value": "[[1, 2, 3], [4, 5, 6]]"}),
              *base[:2], ("r", "model.region", {"kind": "Edge", "method": "Find at"})]
    _, res, inputs = evaluate(points, [("s", "sketch", "p", "profile"), ("p", "part", "r", "source"),
                                        ("pts", "value", "r", "points")])
    assert res["r"]["status"] == "ok"
    assert inputs.outputs["r"]["region"].points == ((1.0, 2.0, 3.0), (4.0, 5.0, 6.0))

    _, res, _ = evaluate([*base[:2], ("r", "model.region", {"kind": "Edge", "method": "Find at"})],
                         [("s", "sketch", "p", "profile"), ("p", "part", "r", "source")])
    assert res["r"]["status"] == "error" and "needs one or more" in res["r"]["message"]


def test_region_generated_code_reports_empty_geometry():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {}),
             ("r", "model.region", {"kind": "Face", "value": "0", "create": "Set", "name": "Loaded"})]
    out, bad = script(nodes, [("s", "sketch", "p", "profile"), ("p", "part", "r", "source")])
    assert not bad
    assert "targets = _geometry(targets, 'Face Region Loaded')" in out["script"]
    assert "else _created_region.faces), 'Face Set Loaded')" in out["script"]
    assert "del owner.sets['Loaded']" in out["script"]
    assert "found no geometry; check the type and Find at points or pick again" in out["script"]


def test_region_find_at_can_use_nearest_geometry_tolerance():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
             ("pts", "input.value", {"type": "List / matrix", "value": "[[1, 2, 3], [4, 5, 6]]"}),
             ("r", "model.region", {"kind": "Face", "method": "Find at", "tolerance": 0.25,
                                      "create": "Set", "name": "Nearest"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "r", "source"),
             ("pts", "value", "r", "points")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "_find_geometry(selected.faces, ((1.0, 2.0, 3.0), (4.0, 5.0, 6.0)), 0.25" in out["script"]
    assert "getClosest(coordinates=tuple(points), searchTolerance=tolerance)" in out["script"]
    assert "matched %d unique geometr" in out["script"]


def test_region_rejects_negative_find_at_tolerance():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {}),
             ("pts", "input.value", {"type": "List / matrix", "value": "[[0, 0, 0]]"}),
             ("r", "model.region", {"kind": "Face", "method": "Find at", "tolerance": -1})]
    _, res, _ = evaluate(nodes, [("s", "sketch", "p", "profile"),
                                  ("p", "part", "r", "source"),
                                  ("pts", "value", "r", "points")])
    assert res["r"]["status"] == "error" and "tolerance" in res["r"]["message"]


def test_instance_can_be_independent_of_its_part():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {}),
             ("i", "model.instance", {"dependency": "Independent"})]
    out, bad = script(nodes, [("s", "sketch", "p", "profile"), ("p", "part", "i", "part")])
    assert not bad
    assert "dependent=OFF" in out["script"]


def test_merge_instances_builds_a_new_part_and_preserves_originals(tmp_path):
    nodes = [("sa", "model.sketch", {}), ("a", "model.part", {"name": "A"}),
             ("ia", "model.instance", {}), ("sb", "model.sketch", {"x": 50}),
             ("b", "model.part", {"name": "B"}), ("ib", "model.instance", {}),
             ("merge", "model.merge_instances", {"name": "Union", "intersections": "Keep"})]
    edges = [("sa", "sketch", "a", "profile"), ("a", "part", "ia", "part"),
             ("sb", "sketch", "b", "profile"), ("b", "part", "ib", "part"),
             ("ia", "instance", "merge", "instances"), ("ib", "instance", "merge", "instances")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    text = out["script"]
    assert text.index("a.Instance(name='A-1'") < text.index("InstanceFromBooleanMerge(name='Union'")
    assert "keepIntersections=ON, originalInstances=SUPPRESS, domain=GEOMETRY" in text
    assert "a.features['A-1'].resume()" in text and "a.features['B-1'].resume()" in text
    log, stdout = run_in_fake_cae(text, tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, "InstanceFromBooleanMerge")


def test_boolean_merge_can_delete_original_instances(tmp_path):
    nodes = [("s", "model.sketch", {}), ("a", "model.part", {"name": "A"}),
             ("ia", "model.instance", {}), ("b", "model.part", {"name": "B"}),
             ("ib", "model.instance", {"x": 25}),
             ("merge", "model.merge_instances", {"name": "Union", "originals": "Delete"})]
    edges = [("s", "sketch", "a", "profile"), ("s", "sketch", "b", "profile"),
             ("a", "part", "ia", "part"), ("b", "part", "ib", "part"),
             ("ia", "instance", "merge", "instances"), ("ib", "instance", "merge", "instances")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    text = out["script"]
    assert "originalInstances=DELETE" in text
    assert ".resume()" not in text
    # Consumed Instances are built inside the Boolean operation, not tracked as permanently missing ledger objects.
    assert not out["node_keys"]["ia"] and not out["node_keys"]["ib"]
    log, stdout = run_in_fake_cae(text, tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, "InstanceFromBooleanMerge")


def test_boolean_merge_can_place_its_result_in_the_assembly(tmp_path):
    nodes = [("s", "model.sketch", {}), ("a", "model.part", {"name": "A"}),
             ("ia", "model.instance", {}), ("b", "model.part", {"name": "B"}),
             ("ib", "model.instance", {"x": 25}),
             ("merge", "model.merge_instances", {"name": "Union", "originals": "Delete",
                                                    "result": "Place in assembly",
                                                    "instance_name": "Joined"})]
    edges = [("s", "sketch", "a", "profile"), ("s", "sketch", "b", "profile"),
             ("a", "part", "ia", "part"), ("b", "part", "ib", "part"),
             ("ia", "instance", "merge", "instances"), ("ib", "instance", "merge", "instances")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    text = out["script"]
    assert "originalInstances=DELETE" in text
    assert text.index("InstanceFromBooleanMerge(name='Union'") < text.index("a.Instance(name='Joined'")
    assert "part=m.parts['Union']" in text
    assert out["node_keys"]["merge"] == ["part|NoCode-Model|Union", "instance|NoCode-Model|Joined"]
    log, stdout = run_in_fake_cae(text, tmp_path)
    assert "failed" not in stdout, stdout
    assert [c["kwargs"]["name"] for c in calls(log, ".Instance")][-1] == "Joined"


def test_boolean_cut_uses_target_and_list_of_cutters(tmp_path):
    nodes = [("s", "model.sketch", {}), ("target_part", "model.part", {"name": "Target"}),
             ("target", "model.instance", {}), ("tool_part", "model.part", {"name": "Tool"}),
             ("tool", "model.instance", {"x": 25}),
             ("cut", "model.merge_instances", {"operation": "Cut", "name": "Cut-Part"})]
    edges = [("s", "sketch", "target_part", "profile"), ("s", "sketch", "tool_part", "profile"),
             ("target_part", "part", "target", "part"), ("tool_part", "part", "tool", "part"),
             ("target", "instance", "cut", "target"), ("tool", "instance", "cut", "tools")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    text = out["script"]
    assert "InstanceFromBooleanCut(name='Cut-Part'" in text
    assert "instanceToBeCut=mdb.models['NoCode-Model'].rootAssembly.instances['Target-1']" in text
    assert "cuttingInstances=(mdb.models['NoCode-Model'].rootAssembly.instances['Tool-1'],)" in text
    log, stdout = run_in_fake_cae(text, tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, "InstanceFromBooleanCut")


def test_merge_ignores_a_target_left_wired_from_cut():
    nodes = [("s", "model.sketch", {}), ("a_part", "model.part", {"name": "A"}), ("a", "model.instance", {}),
             ("b_part", "model.part", {"name": "B"}), ("b", "model.instance", {"x": 25}),
             ("c_part", "model.part", {"name": "C"}), ("c", "model.instance", {"x": 50}),
             ("m", "model.merge_instances", {"operation": "Merge", "name": "Union"})]
    edges = [("s", "sketch", "a_part", "profile"), ("s", "sketch", "b_part", "profile"),
             ("s", "sketch", "c_part", "profile"), ("a_part", "part", "a", "part"),
             ("b_part", "part", "b", "part"), ("c_part", "part", "c", "part"),
             ("a", "instance", "m", "instances"), ("b", "instance", "m", "instances"),
             ("c", "instance", "m", "target")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    text = out["script"]
    assert "InstanceFromBooleanMerge(name='Union'" in text
    assert "a.features['C-1'].resume()" not in text


def test_picked_part_is_modified_by_reversible_features_without_a_copy():
    nodes = [("p", "input.value", {"type": "Part in CAE", "value": "Hand Model|Original"}),
             ("f", "model.feature", {"name": "Editable", "offset": 5})]
    out, bad = script(nodes, [("p", "value", "f", "part")])
    assert not bad, bad
    assert "objectToCopy" not in out["script"]
    assert "feature|Hand Model|Original|Editable" in out["script"]
    assert "part.features.changeKey(fromName=_feature.name, toName='Editable')" in out["script"]
    assert "del m.parts['Original']" not in out["script"]


@pytest.mark.parametrize(("operation", "call", "argument"),
                         [("Fillet", "Round", "radius=2.5"), ("Chamfer", "Chamfer", "length=2.5")])
def test_part_feature_rounds_or_chamfers_edge_regions(tmp_path, operation, call, argument):
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
             ("r", "model.region", {"kind": "Edge", "value": "0, 1"}),
             ("f", "model.feature", {"operation": operation, "radius": 2.5, "name": operation + " edges"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "r", "source"),
             ("p", "part", "f", "part"), ("r", "region", "f", "region")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert f"part.{call}({argument}, edgeList=tuple(targets))" in out["script"]
    assert f"feature|NoCode-Model|Part-1|{operation}-edges" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, call)


@pytest.mark.parametrize(("operation", "call"), [("Extrude add", "SolidExtrude"),
                                                   ("Extrude cut", "CutExtrude")])
def test_part_feature_adds_or_cuts_a_closed_sketch_from_one_face(tmp_path, operation, call):
    nodes = [("base", "model.sketch", {}),
             ("part", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("face", "model.region", {"kind": "Face", "value": "0"}),
             ("profile", "model.sketch", {"shape": "Circle", "radius": 2}),
             ("feature", "model.feature", {"operation": operation, "depth": 5, "name": "Boss-or-hole"})]
    edges = [("base", "sketch", "part", "profile"), ("part", "part", "face", "source"),
             ("part", "part", "feature", "part"), ("face", "region", "feature", "region"),
             ("profile", "sketch", "feature", "sketch")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "MakeSketchTransform(sketchPlane=sketch_plane" in out["script"]
    assert f"part.{call}(" in out["script"] and "depth=5.0" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, call)


def test_sketch_arc_and_spline_are_native_cae_geometry(tmp_path):
    nodes = [("arc", "model.sketch", {"shape": "Arc", "x": 2, "y": 3, "radius": 4,
                                        "start": 0, "end": 90, "direction": "Counterclockwise"}),
             ("spline", "model.sketch", {"shape": "Spline", "points": "0,0; 5,8; 10,0",
                                           "closed": "Closed"})]
    out, bad = script(nodes, [])
    assert not bad, bad
    assert ".ArcByCenterEnds(center=(2.0, 3.0), point1=(6.0, 3.0)" in out["script"]
    assert "direction=COUNTERCLOCKWISE" in out["script"]
    assert ".Spline(points=((0.0, 0.0), (5.0, 8.0), (10.0, 0.0)))" in out["script"]
    assert ".Line(point1=(10.0, 0.0), point2=(0.0, 0.0))" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, ".ArcByCenterEnds") and calls(log, ".Spline")


def test_datum_node_feeds_part_feature_and_has_own_lifecycle(tmp_path):
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude"}),
             ("d", "model.datum", {"name": "Midplane", "plane": "YZ", "offset": 25}),
             ("f", "model.feature", {"name": "Split"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "d", "part"),
             ("p", "part", "f", "part"), ("d", "datum", "f", "datum")]
    first, bad = script(nodes, edges)
    assert not bad, bad
    assert "datum|NoCode-Model|Part-1|Midplane" in first["script"]
    assert "feature|NoCode-Model|Part-1|Split" in first["script"]
    log, stdout = run_in_fake_cae(first["script"], tmp_path)
    assert "failed" not in stdout, stdout

    second, bad = script(nodes[:-1], edges[:-2], previous=first)
    assert not bad, bad
    log, stdout = run_in_fake_cae([first["script"], second["script"]], tmp_path)
    deleted = [c["args"] for c in runs(log)[1] if c["call"].startswith("del ") and c["call"].endswith("features")]
    assert deleted == [["Split"]]  # Datum remains because its node remains.


def test_one_datum_node_covers_points_axes_and_three_point_planes():
    common = [("s", "model.sketch", {}), ("p", "model.part", {})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "d", "part"),
             ("pts", "value", "d", "points")]
    cases = [
        ("Point", "0, 1, 2", "DatumPointByCoordinate(coords=(0.0, 1.0, 2.0))"),
        ("Axis through 2 points", "0,0,0; 0,0,1", "DatumAxisByTwoPoint"),
        ("Plane through 3 points", "0,0,0; 1,0,0; 0,1,0", "DatumPlaneByThreePoints"),
    ]
    for kind, points, expected in cases:
        out, bad = script([*common, ("pts", "input.value", {"type": "Text", "value": points}),
                           ("d", "model.datum", {"kind": kind})], edges)
        assert not bad, (kind, bad)
        assert expected in out["script"]

    out, bad = script([*common, ("d", "model.datum", {"kind": "Principal axis", "axis": "X"})],
                      [("s", "sketch", "p", "profile"), ("p", "part", "d", "part")])
    assert not bad and "DatumAxisByPrincipalAxis(principalAxis=XAXIS)" in out["script"]


def test_datum_can_be_created_in_the_assembly_from_a_model_or_instance(tmp_path):
    nodes = [("m", "model.model", {"name": "Assembly Datums"}), ("s", "model.sketch", {}),
             ("p", "model.part", {}), ("i", "model.instance", {"name": "Placed"}),
             ("global", "model.datum", {"name": "Global XY", "offset": 4}),
             ("on_i", "model.datum", {"name": "On instance", "kind": "Principal axis", "axis": "X"})]
    edges = [("m", "model", "p", "model"), ("s", "sketch", "p", "profile"),
             ("p", "part", "i", "part"), ("m", "model", "global", "part"),
             ("i", "instance", "on_i", "part")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "datum|Assembly-Datums|assembly|Global-XY" in out["script"]
    assert "datum|Assembly-Datums|assembly|On-instance" in out["script"]
    assert "owner = a" in out["script"] and "owner.DatumPlaneByPrincipalPlane" in out["script"]
    assert "owner.DatumAxisByPrincipalAxis" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, "rootAssembly.DatumPlane") and calls(log, "rootAssembly.DatumAxis")


@pytest.mark.parametrize(("suffix", "opener"), [(".step", "openStep"), (".igs", "openIges"), (".sat", "openAcis")])
def test_part_imports_cad_geometry(tmp_path, suffix, opener):
    cad = tmp_path / ("shape" + suffix)
    cad.write_text("fake geometry")
    nodes = [("p", "model.part", {"method": "Import CAD", "file": str(cad), "name": "Imported"})]
    out, bad = script(nodes, [])
    assert not bad, bad
    assert f"mdb.{opener}(fileName={str(cad)!r}" in out["script"]
    assert "PartFromGeometryFile(name='Imported'" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, opener) and calls(log, "PartFromGeometryFile")


def test_job_writes_input_and_returns_odb_path(tmp_path):
    nodes = [("m", "model.model", {"name": "Analysis"}),
             ("st", "model.step", {"name": "Load"}),
             ("j", "model.job", {"name": "Solve", "directory": str(tmp_path), "cpus": 4,
                                   "memory": 75, "action": "Write input only"})]
    edges = [("m", "model", "st", "after"), ("st", "step", "j", "analysis")]
    g, res, inputs = evaluate(nodes, edges)
    assert all(v["status"] == "ok" for v in res.values()), res
    assert inputs.outputs["j"]["path"].value == str(tmp_path / "Solve.odb")
    out = build_script(g, res, inputs)
    assert "mdb.Job(name='Solve', model='Analysis', numCpus=4, numDomains=4, memory=75" in out["script"]
    assert "_job.writeInput(consistencyChecking=OFF)" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, "mdb.Job") and calls(log, "mdb.jobs[Solve].writeInput")


def test_cae_never_submits_jobs_and_old_actions_still_load(tmp_path):
    for old, now in (("Submit", "Run on request"), ("Submit and wait", "Run automatically"),
                     ("Write input", "Write input only")):
        nodes = [("m", "model.model", {}), ("j", "model.job", {"action": old, "directory": str(tmp_path)})]
        g, res, inputs = evaluate(nodes, [("m", "model", "j", "analysis")])
        assert res["j"]["status"] == "ok", res["j"]
        assert inputs.outputs["j"]["job"].action == now
        out = build_script(g, res, inputs)
        assert "submit(" not in out["script"] and "waitForCompletion" not in out["script"]
        assert "_job.writeInput(consistencyChecking=OFF)" in out["script"]


def test_job_input_is_not_rewritten_while_it_runs(tmp_path):
    (tmp_path / "Busy.lck").write_text("")
    nodes = [("m", "model.model", {}), ("j", "model.job", {"name": "Busy", "directory": str(tmp_path)})]
    out, bad = script(nodes, [("m", "model", "j", "analysis")])
    assert not bad, bad
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "job Busy is running" in stdout
    assert not calls(log, "writeInput")


def test_job_default_directory_is_not_the_server_folder():
    from nodecode_fea import jobs
    for typed in ("", "."):
        g, res, inputs = evaluate([("m", "model.model", {}), ("j", "model.job", {"directory": typed})],
                                  [("m", "model", "j", "analysis")])
        assert inputs.outputs["j"]["job"].directory == str(jobs.DEFAULT_DIRECTORY)


def test_measure_queries_cae_then_uses_cached_number():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
             ("r", "model.region", {"kind": "Face", "value": "0, 1"}),
             ("m", "model.measure", {"quantity": "Area"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "r", "source"),
             ("r", "region", "m", "first")]
    g, res, inputs = evaluate(nodes, edges)
    pending = inputs.outputs["m"]["value"]
    assert isinstance(pending, PendingMeasure)
    out = build_script(g, res, inputs)
    assert ".getArea(faces=first)" in out["script"] and cae_queries.MARKER in out["script"]
    assert cae_queries.ingest(cae_queries.MARKER + '{"%s": 123.5}' % pending.key)
    _, res, inputs = evaluate(nodes, edges)
    assert res["m"]["status"] == "ok"
    assert inputs.outputs["m"]["value"] == Number(123.5, "Area")


def test_measure_of_a_region_before_a_part_feature_runs_on_a_copy_without_it():
    # the Defeature example: the holes are counted on the part before Remove faces fills them
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
             ("r", "model.region", {"method": "By shape"}), ("m", "model.measure", {"quantity": "Count"}),
             ("f", "model.feature", {"operation": "Remove faces", "name": "No holes"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "r", "source"), ("r", "region", "m", "first"),
             ("p", "part", "f", "part"), ("r", "region", "f", "region")]
    g, res, inputs = evaluate(nodes, edges)
    script = build_script(g, res, inputs)["script"]
    assert "Part(name='__nocode_stage', objectToCopy=mdb.models['NoCode-Model'].parts['Part-1'])" in script
    assert "for _f in ['No-holes']:" in script and "_find_geometry(_stage.faces" in script
    assert "del mdb.models['NoCode-Model'].parts['__nocode_stage']" in script
    # measured on the final part: as before
    edges[1:] = [("p", "part", "f", "part"), ("f", "part", "r", "source"), ("r", "region", "m", "first")]
    g, res, inputs = evaluate(nodes[:3] + nodes[3:], edges)
    assert "__nocode_stage" not in build_script(g, res, inputs)["script"]


def test_region_and_measure_map_lists_of_point_tables():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
             ("pts", "input.value", {"type": "List / matrix",
                                      "value": "[[[1, 2, 0]], [[3, 4, 0], [5, 6, 0]]]"}),
             ("r", "model.region", {"kind": "Face", "method": "Find at"}),
             ("m", "model.measure", {"quantity": "Area"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "r", "source"),
             ("pts", "value", "r", "points"), ("r", "region", "m", "first")]
    _, res, inputs = evaluate(nodes, edges)
    assert res["r"]["status"] == "ok" and res["m"]["status"] == "ok"
    regions = inputs.outputs["r"]["region"]
    assert [len(r.points) for r in regions] == [1, 2]
    assert all(isinstance(v, PendingMeasure) for v in inputs.outputs["m"]["value"])


def test_one_center_per_mapped_instance_is_a_plain_n_by_3_matrix(monkeypatch):
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
             ("a", "model.instance", {"name": "A"}), ("b", "model.instance", {"name": "B", "x": 100}),
             ("instances", "list.merge", {}), ("r", "model.region", {"kind": "Face", "value": "0"}),
             ("center", "model.measure", {"quantity": "Center"}),
             ("x", "math.formula", {"expr": "a[:, 0]"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "a", "part"),
             ("p", "part", "b", "part"), ("a", "instance", "instances", "a"),
             ("b", "instance", "instances", "b"), ("instances", "list", "r", "source"),
             ("r", "region", "center", "first"), ("center", "value", "x", "a")]

    monkeypatch.setattr(cae_queries, "value", lambda quantity, first, second=None:
                        [[1.0 if first.source.name == "A" else 101.0, 2.0, 3.0]])
    _, res, inputs = evaluate(nodes, edges)
    assert res["center"]["status"] == "ok" and res["center"]["shape"] == [2, 3], res["center"]
    assert res["x"]["status"] == "ok", res["x"]
    assert [n.value for n in inputs.outputs["x"]["value"]] == [1.0, 101.0]


def test_region_creates_named_part_set_and_instance_surface(tmp_path):
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
             ("set", "model.region", {"kind": "Cell", "value": "0", "create": "Set", "name": "Core"}),
             ("i", "model.instance", {}),
             ("surf", "model.region", {"kind": "Face", "value": "0, 1", "create": "Surface",
                                        "name": "Loaded", "side": "Side 2"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "set", "source"),
             ("p", "part", "i", "part"), ("i", "instance", "surf", "source")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "owner.Set(name='Core', **_region_kw(selected, 'cells', targets, 'cells'))" in out["script"]
    assert "owner.Surface(name='Loaded', **_region_kw(selected, 'faces', targets, 'side2Faces'))" in out["script"]
    assert "region|NoCode-Model|part|Part-1|set|Core" in out["script"]
    assert "region|NoCode-Model|assembly|rootAssembly|surface|Loaded" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, ".Set") and calls(log, ".Surface")


def test_region_can_combine_a_mapped_instance_list_into_one_assembly_set(tmp_path):
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
             ("left", "model.instance", {"name": "Left"}),
             ("right", "model.instance", {"name": "Right", "x": 100}),
             ("instances", "list.merge", {}),
             ("set", "model.region", {"kind": "Face", "value": "0", "create": "Set", "name": "Ends",
                                         "group": "One combined assembly region"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "left", "part"),
             ("p", "part", "right", "part"), ("left", "instance", "instances", "a"),
             ("right", "instance", "instances", "b"), ("instances", "list", "set", "source")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert out["script"].count("region|NoCode-Model|assembly|rootAssembly|set|Ends") == 1
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    made = calls(log, "rootAssembly.Set")
    assert len(made) == 1 and made[0]["kwargs"]["name"] == "Ends"


def test_if_filters_region_members_then_region_creates_a_set():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
             ("faces", "model.region", {"kind": "Face", "value": "0, 1, 2"}),
             ("yes1", "input.value", {"type": "Yes/No", "value": "Yes"}),
             ("no", "input.value", {"type": "Yes/No", "value": "No"}),
             ("yes2", "input.value", {"type": "Yes/No", "value": "Yes"}),
             ("flags", "list.merge", {}), ("keep", "logic.if", {}),
             ("set", "model.region", {"create": "Set", "name": "Large faces"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "faces", "source"),
             ("yes1", "value", "flags", "a"), ("no", "value", "flags", "b"),
             ("yes2", "value", "flags", "c"), ("flags", "list", "keep", "condition"),
             ("faces", "region", "keep", "then"), ("keep", "result", "set", "source")]
    g, res, inputs = evaluate(nodes, edges)
    assert res["keep"]["status"] == "ok" and res["set"]["status"] == "ok", res
    assert inputs.outputs["keep"]["result"].positions == (0, 2)
    assert inputs.outputs["keep"]["result"].position_count == 3
    assert inputs.outputs["set"]["region"].name == "Large-faces"
    text = build_script(g, res, inputs)["script"]
    assert "_region_positions(targets, (0, 2), 3, 'Face Region Large-faces')" in text
    assert "owner.Set(name='Large-faces'" in text


def test_filtered_region_list_can_make_one_part_set():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
             ("r0", "model.region", {"kind": "Face", "value": "0"}),
             ("r1", "model.region", {"kind": "Face", "value": "1"}),
             ("r2", "model.region", {"kind": "Face", "value": "2"}),
             ("regions", "list.merge", {}),
             ("yes1", "input.value", {"type": "Yes/No", "value": "Yes"}),
             ("no", "input.value", {"type": "Yes/No", "value": "No"}),
             ("yes2", "input.value", {"type": "Yes/No", "value": "Yes"}),
             ("flags", "list.merge", {}), ("keep", "logic.if", {}),
             ("set", "model.region", {"create": "Set", "name": "Large faces",
                                         "group": "One combined assembly region"})]
    edges = [("s", "sketch", "p", "profile"),
             *(("p", "part", rid, "source") for rid in ("r0", "r1", "r2")),
             ("r0", "region", "regions", "a"), ("r1", "region", "regions", "b"),
             ("r2", "region", "regions", "c"),
             ("yes1", "value", "flags", "a"), ("no", "value", "flags", "b"),
             ("yes2", "value", "flags", "c"), ("flags", "list", "keep", "condition"),
             ("regions", "list", "keep", "then"), ("keep", "result", "set", "source")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert out["script"].count("region|NoCode-Model|part|Part-1|set|Large-faces") == 1
    assert out["script"].count("owner.Set(name='Large-faces'") == 1


def test_surface_rejects_cells_and_vertices():
    for kind in ("Cell", "Vertex"):
        nodes = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
                 ("r", "model.region", {"kind": kind, "value": "0", "create": "Surface"})]
        _, res, _ = evaluate(nodes, [("s", "sketch", "p", "profile"), ("p", "part", "r", "source")])
        assert res["r"]["status"] == "error" and "faces or edges" in res["r"]["message"]


def test_mesh_seeds_types_controls_and_generates(tmp_path):
    nodes = [("s", "model.sketch", {}),
             ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 20}),
             ("mesh", "model.mesh", {"size": 4, "technique": "Free", "shape": "Tet"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "mesh", "target")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "seedPart(size=4.0" in out["script"]
    assert "setMeshControls(regions=part.cells[:], technique=FREE, elemShape=TET)" in out["script"]
    assert "mesh.ElemType(elemCode=C3D4, elemLibrary=STANDARD)" in out["script"]
    assert "part.generateMesh()" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, ".seedPart") and calls(log, ".setMeshControls")
    assert calls(log, "mesh.ElemType") and calls(log, ".setElementType") and calls(log, ".generateMesh")


def test_mesh_edge_region_is_locally_seeded_and_lists_map():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
             ("r", "model.region", {"kind": "Edge", "value": "@0,0,0;10,0,0"}),
             ("sizes", "input.value", {"type": "List / matrix", "value": "2, 5"}),
             ("mesh", "model.mesh", {"generate": "No"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "r", "source"),
             ("r", "region", "mesh", "target"), ("sizes", "value", "mesh", "param:size")]
    g, res, inputs = evaluate(nodes, edges)
    assert res["mesh"]["status"] == "ok"
    parts = inputs.outputs["mesh"]["part"]
    assert [p.features[-1][2] for p in parts] == [2.0, 5.0]
    out = build_script(g, res, inputs)
    assert out["script"].count("seedEdgeBySize") == 2
    assert "part.generateMesh()" not in out["script"]


def test_mesh_rejects_wrong_shape_and_feature_after_mesh():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {}),
             ("mesh", "model.mesh", {"shape": "Hex"}), ("f", "model.feature", {})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "mesh", "target"),
             ("mesh", "part", "f", "part")]
    _, res, _ = evaluate(nodes, edges)
    assert res["mesh"]["status"] == "error" and "Shell" in res["mesh"]["message"]

    nodes[2] = ("mesh", "model.mesh", {})
    _, res, _ = evaluate(nodes, edges)
    assert res["f"]["status"] == "error" and "before Mesh" in res["f"]["message"]


@pytest.mark.parametrize("body,part_params,section_call,assignment_repo", [
    ("Solid", {"body": "Solid", "method": "Extrude"}, "HomogeneousSolidSection", "cells"),
    ("Shell", {}, "HomogeneousShellSection", "faces"),
    ("Wire", {"body": "Wire"}, "BeamSection", "edges"),
])
def test_material_and_section_are_defined_and_assigned(body, part_params, section_call, assignment_repo, tmp_path):
    nodes = [("s", "model.sketch", {"shape": "Profile", "points": "0,0; 20,0; 20,20",
                                     **({"closed": "Closed"} if body != "Wire" else {})}),
             ("p", "model.part", part_params),
             ("sec", "model.section", {"material": "Steel", "section": body + "Section", "density": 7.85e-9})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "sec", "target")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "material.Elastic" in out["script"] and "material.Density" in out["script"]
    assert section_call in out["script"]
    assert f"regionToolset.Region({assignment_repo}=section_targets)" in out["script"]
    if body == "Wire":
        assert "assignBeamSectionOrientation" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, ".Material") and calls(log, ".Elastic") and calls(log, ".SectionAssignment")


def test_section_region_and_property_lists_map():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude"}),
             ("r", "model.region", {"kind": "Cell", "value": "0"}),
             ("e", "input.value", {"type": "List / matrix", "value": "70000, 210000"}),
             ("sec", "model.section", {"material": "Alloy", "section": "Core"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "r", "source"),
             ("r", "region", "sec", "target"), ("e", "value", "sec", "param:young")]
    _, res, inputs = evaluate(nodes, edges)
    assert res["sec"]["status"] == "ok"
    parts = inputs.outputs["sec"]["part"]
    assert [p.sections[-1][0].material.young for p in parts] == [70000.0, 210000.0]
    assert [p.sections[-1][0].name for p in parts] == ["Core-1", "Core-2"]


def test_analysis_steps_chain_and_build_in_cae(tmp_path):
    nodes = [("m", "model.model", {}), ("static", "model.step", {"name": "Load", "nlgeom": "Yes"}),
             ("dynamic", "model.step", {"name": "Ringdown", "kind": "Dynamic implicit", "time": 2}),
             ("freq", "model.step", {"name": "Modes", "kind": "Frequency", "modes": 12})]
    edges = [("m", "model", "static", "after"), ("static", "step", "dynamic", "after"),
             ("static", "step", "freq", "after")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "m.StaticStep(name='Load', previous='Initial'" in out["script"]
    assert "m.ImplicitDynamicsStep(name='Ringdown', previous='Load'" in out["script"]
    assert "m.FrequencyStep(name='Modes', previous='Load', numEigen=12)" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, ".StaticStep") and calls(log, ".ImplicitDynamicsStep") and calls(log, ".FrequencyStep")


def test_explicit_steps_chain_only_with_explicit_steps(tmp_path):
    nodes = [("m", "model.model", {}),
             ("drop", "model.step", {"name": "Drop", "kind": "Dynamic explicit", "time": 0.002, "nlgeom": "Yes",
                                     "scale": 0.9, "mass_dt": 1e-7}),
             ("rebound", "model.step", {"name": "Rebound", "kind": "Dynamic explicit", "time": 0.001})]
    edges = [("m", "model", "drop", "after"), ("drop", "step", "rebound", "after")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert ("m.ExplicitDynamicsStep(name='Drop', previous='Initial', timePeriod=0.002, nlgeom=ON, scaleFactor=0.9, "
            "massScaling=((SEMI_AUTOMATIC, MODEL, AT_BEGINNING, 0.0, 1e-07, BELOW_MIN, 0, 0, 0.0, 0.0, 0, None),))"
            in out["script"])
    assert ("m.ExplicitDynamicsStep(name='Rebound', previous='Drop', timePeriod=0.001, nlgeom=OFF, scaleFactor=1.0)"
            in out["script"])
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert len(calls(log, ".ExplicitDynamicsStep")) == 2

    # Abaqus refuses an Explicit step after a Standard one and the other way round ("Invalid sequence of steps")
    for first, second in (("Static", "Dynamic explicit"), ("Dynamic explicit", "Static"),
                          ("Dynamic explicit", "Frequency")):
        _, res, _ = evaluate([("m", "model.model", {}), ("a", "model.step", {"kind": first}),
                              ("b", "model.step", {"name": "Next", "kind": second})],
                             [("m", "model", "a", "after"), ("a", "step", "b", "after")])
        assert res["b"]["status"] == "error" and "can't follow each other" in res["b"]["message"], (first, second)
    for bad_setting in ({"scale": 0}, {"mass_dt": -1}):
        _, res, _ = evaluate([("s", "model.step", dict({"kind": "Dynamic explicit"}, **bad_setting))], [])
        assert res["s"]["status"] == "error" and "Time scaling factor" in res["s"]["message"]


def test_contact_in_an_explicit_model_uses_explicit_contact(tmp_path):
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("i", "model.instance", {}), ("a", "model.region", {"kind": "Face", "value": "0"}),
             ("b", "model.region", {"kind": "Face", "value": "1"}),
             ("step", "model.step", {"name": "Impact", "kind": "Dynamic explicit", "time": 0.01}),
             ("all", "model.interaction", {"name": "Everything", "kind": "General contact", "friction": 0.3}),
             ("rub", "model.interaction", {"name": "Rub", "kind": "Frictional contact", "friction": 0.2,
                                           "sliding": "Small", "adjust": "Yes"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "i", "part"), ("i", "instance", "a", "source"),
             ("i", "instance", "b", "source"), ("step", "step", "all", "step"), ("step", "step", "rub", "step"),
             ("a", "region", "rub", "master"), ("b", "region", "rub", "slave")]
    # the contacts placed before the step in the graph still know the model is explicit
    nodes = nodes[:5] + nodes[6:] + nodes[5:6]
    out, bad = script(nodes, edges)
    assert not bad, bad
    code = out["script"]
    assert "m.ContactExp(name='Everything', createStepName='Initial')" in code and "ContactStd" not in code
    assert ("m.SurfaceToSurfaceContactExp('Rub', 'Impact', master_surface, slave_surface, sliding=SMALL, "
            "interactionProperty='Rub-Property')") in code
    log, stdout = run_in_fake_cae(code, tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, ".ContactExp") and calls(log, ".SurfaceToSurfaceContactExp")

    # switching the step to Standard rebuilds the contacts as Standard ones
    nodes[-1] = ("step", "model.step", {"name": "Impact"})
    out, bad = script(nodes, edges)
    assert "m.ContactStd(name='Everything'" in out["script"] and "SurfaceToSurfaceContactStd('Rub'" in out["script"]


def test_step_settings_map_lists_and_validate():
    nodes = [("times", "input.value", {"type": "List / matrix", "value": "1, 2, 5"}),
             ("step", "model.step", {})]
    _, res, inputs = evaluate(nodes, [("times", "value", "step", "param:time")])
    assert res["step"]["status"] == "ok"
    steps = inputs.outputs["step"]["step"]
    assert [s.name for s in steps] == ["Step-1-1", "Step-1-2", "Step-1-3"]
    assert [s.time for s in steps] == [1.0, 2.0, 5.0]

    _, res, _ = evaluate([("step", "model.step", {"time": 0})], [])
    assert res["step"]["status"] == "error" and "positive" in res["step"]["message"]

    nodes = [("d", "model.step", {"kind": "Dynamic implicit"}),
             ("f", "model.step", {"kind": "Frequency"})]
    _, res, _ = evaluate(nodes, [("d", "step", "f", "after")])
    assert res["f"]["status"] == "error" and "perturbation" in res["f"]["message"]


def test_loads_and_boundary_conditions_chain_and_build_in_cae(tmp_path):
    nodes = [("s", "model.sketch", {}),
             ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("face", "model.region", {"kind": "Face", "value": "0"}),
             ("vertex", "model.region", {"kind": "Vertex", "value": "0"}),
             ("step", "model.step", {"name": "Loading"}),
             ("fixed", "model.condition", {"name": "Clamp", "kind": "Fixed"}),
             ("pressure", "model.condition", {"name": "Push", "kind": "Pressure", "magnitude": 12.5}),
             ("force", "model.condition", {"name": "Point load", "kind": "Concentrated force",
                                               "x": 4, "y": -2, "z": 0}),
             ("gravity", "model.condition", {"name": "Weight", "kind": "Gravity", "z": -9810}),
             ("i", "model.instance", {})]  # the part's Regions go on its instance
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "i", "part"), ("p", "part", "face", "source"),
             ("p", "part", "vertex", "source"), ("step", "step", "fixed", "step"),
             ("face", "region", "fixed", "region"), ("fixed", "condition", "pressure", "step"),
             ("face", "region", "pressure", "region"), ("pressure", "condition", "force", "step"),
             ("vertex", "region", "force", "region"), ("force", "condition", "gravity", "step")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "m.EncastreBC(name='Clamp'" in out["script"]
    assert "m.Pressure(name='Push'" in out["script"] and "magnitude=12.5" in out["script"]
    assert "m.ConcentratedForce(name='Point-load'" in out["script"]
    assert "m.Gravity(name='Weight'" in out["script"] and "comp3=-9810.0" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, ".EncastreBC") and calls(log, ".Pressure")
    assert calls(log, ".ConcentratedForce") and calls(log, ".Gravity")


def test_condition_settings_map_lists_and_validate():
    nodes = [("step", "model.step", {}),
             ("values", "input.value", {"type": "List / matrix", "value": "1, 2, 3"}),
             ("gravity", "model.condition", {"kind": "Gravity"})]
    _, res, inputs = evaluate(nodes, [("step", "step", "gravity", "step"),
                                           ("values", "value", "gravity", "param:z")])
    assert res["gravity"]["status"] == "ok", res["gravity"]
    assert [c.name for c in inputs.outputs["gravity"]["condition"]] == [
        "Condition-1-1", "Condition-1-2", "Condition-1-3"]

    _, res, _ = evaluate([("step", "model.step", {}),
                          ("bad", "model.condition", {"kind": "Pressure"})],
                         [("step", "step", "bad", "step")])
    assert res["bad"]["status"] == "error" and "Region" in res["bad"]["message"]

    nodes = [("step", "model.step", {}), ("s", "model.sketch", {}), ("p", "model.part", {}),
             ("edge", "model.region", {"kind": "Edge", "value": "0"}),
             ("bad", "model.condition", {"kind": "Pressure"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "edge", "source"),
             ("step", "step", "bad", "step"), ("edge", "region", "bad", "region")]
    _, res, _ = evaluate(nodes, edges)
    assert res["bad"]["status"] == "error" and "Face Region" in res["bad"]["message"]

    nodes = [("step", "model.step", {}), ("s", "model.sketch", {}), ("p", "model.part", {}),
             ("face", "model.region", {"kind": "Face", "value": "0"}),
             ("move", "model.condition", {"kind": "Displacement / rotation", "u1": "2.5"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "face", "source"),
             ("step", "step", "move", "step"), ("face", "region", "move", "region")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "u1=2.5, u2=UNSET, u3=UNSET, ur1=UNSET, ur2=UNSET, ur3=UNSET" in out["script"]

    nodes[-1] = ("move", "model.condition", {"kind": "Displacement / rotation"})
    _, res, _ = evaluate(nodes, edges)
    assert res["move"]["status"] == "error" and "at least one" in res["move"]["message"]


def test_tie_and_contact_interactions_chain_and_build_in_cae(tmp_path):
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {}),
             ("master", "model.region", {"kind": "Face", "value": "0"}),
             ("slave", "model.region", {"kind": "Face", "value": "1"}),
             ("step", "model.step", {}), ("tie", "model.interaction", {"name": "Bond"}),
             ("contact", "model.interaction", {"name": "Rub", "kind": "Frictional contact",
                                                  "friction": 0.3, "sliding": "Small", "adjust": "No"}),
             ("i", "model.instance", {})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "i", "part"), ("p", "part", "master", "source"),
             ("p", "part", "slave", "source"), ("step", "step", "tie", "step"),
             ("master", "region", "tie", "master"), ("slave", "region", "tie", "slave"),
             ("tie", "interaction", "contact", "step"),
             ("master", "region", "contact", "master"), ("slave", "region", "contact", "slave")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "m.Tie('Bond', master_surface, slave_surface" in out["script"]
    assert "m.SurfaceToSurfaceContactStd('Rub', 'Step-1', master_surface, slave_surface" in out["script"]
    assert "table=((0.3,),)" in out["script"] and "sliding=SMALL" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, ".Tie") and calls(log, ".ContactProperty")
    assert calls(log, ".SurfaceToSurfaceContactStd")


def test_interaction_validates_regions_and_maps_lists():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {}),
             ("face", "model.region", {"kind": "Face", "value": "0"}),
             ("edge", "model.region", {"kind": "Edge", "value": "0"}),
             ("step", "model.step", {}), ("i", "model.interaction", {})]
    common = [("s", "sketch", "p", "profile"), ("p", "part", "face", "source"),
              ("p", "part", "edge", "source"), ("step", "step", "i", "step"),
              ("face", "region", "i", "master")]
    _, res, _ = evaluate(nodes, common + [("edge", "region", "i", "slave")])
    assert res["i"]["status"] == "error" and "Face Regions" in res["i"]["message"]

    nodes += [("mu", "input.value", {"type": "List / matrix", "value": "0.1, 0.2"})]
    edges = common + [("face", "region", "i", "slave"), ("mu", "value", "i", "param:friction")]
    nodes[5] = ("i", "model.interaction", {"kind": "Frictional contact"})
    _, res, inputs = evaluate(nodes, edges)
    assert res["i"]["status"] == "ok", res["i"]
    assert [i.friction for i in inputs.outputs["i"]["interaction"]] == [0.1, 0.2]


@pytest.mark.parametrize("sketch, part, error", [
    ({}, {"body": "Solid"}, "needs Extrude or Revolve"),
    ({"shape": "Profile", "points": "0,0; 100,0; 100,50"}, {}, "closed profile"),
    ({}, {"body": "Wire", "method": "Extrude"}, "use Method Planar"),
    ({"x": -10}, {"method": "Revolve"}, "x ≥ 0"),
    ({"width": 0}, {}, "positive"),
])
def test_part_errors(sketch, part, error):
    _, res, _ = evaluate(*chain(sketch, part))
    bad = [r["message"] for r in res.values() if r["status"] == "error"]
    assert bad and error in bad[0], res


def test_sketch_adds_holes_and_previews(tmp_path):
    nodes = [("hole", "model.sketch", {"name": "Hole", "shape": "Circle", "x": 50, "y": 25, "radius": 10}),
             ("plate", "model.sketch", {}), ("p", "model.part", {})]
    edges = [("hole", "sketch", "plate", "add"), ("plate", "sketch", "p", "profile")]
    _, res, _ = evaluate(nodes, edges)
    d = res["plate"]["display"]
    assert d["kind"] == "sketch" and len(d["lines"]) == 2
    out, _ = script(nodes, edges)
    log, _ = run_in_fake_cae(out["script"], tmp_path)
    assert calls(log, ".rectangle") and calls(log, ".CircleByCenterPerimeter") and calls(log, "BaseShell")
    # both sketches are kept in CAE by name; the part copies its own
    made = [c["kwargs"]["name"] for c in calls(log, ".ConstrainedSketch")]
    assert {"Hole", "Sketch-1", "__nocode_profile"} <= set(made)


def test_only_what_changed_is_rebuilt(tmp_path):
    nodes = [("m", "model.model", {}),
             ("sa", "model.sketch", {}), ("a", "model.part", {"name": "A"}), ("ia", "model.instance", {}),
             ("sb", "model.sketch", {"name": "SB", "shape": "Circle"}), ("b", "model.part", {"name": "B"}),
             ("ib", "model.instance", {})]
    edges = [("m", "model", "a", "model"), ("m", "model", "b", "model"),
             ("sa", "sketch", "a", "profile"), ("a", "part", "ia", "part"),
             ("sb", "sketch", "b", "profile"), ("b", "part", "ib", "part")]
    first, _ = script(nodes, edges)
    again, _ = script(nodes, edges, previous=first["fingerprints"])
    nodes[1] = ("sa", "model.sketch", {"width": 300})  # change A's sketch only
    changed, _ = script(nodes, edges, previous=first["fingerprints"])
    log, stdout = run_in_fake_cae([first["script"], again["script"], changed["script"]], tmp_path)
    r1, r2, r3 = runs(log)
    assert len(calls(r1, "mdb.Model")) == 1 and len(calls(r1, ".Part")) == 2 and len(calls(r1, ".Instance")) == 2
    assert not calls(r2, "mdb.Model") and not calls(r2, ".Part") and not calls(r2, ".Instance")  # nothing changed
    assert again["module"] == ""  # nothing to show: CAE stays where it is
    # A's sketch changed: A and its instance are rebuilt; the model, B and B's instance are left alone
    assert not calls(r3, "mdb.Model")
    assert [c["kwargs"]["name"] for c in calls(r3, ".Part")] == ["A"]
    assert [c["kwargs"]["name"] for c in calls(r3, ".Instance")] == ["A-1"]
    assert [c["args"] for c in calls(r3, "del model[NoCode-Model].parts")] == [["A"]]
    assert changed["module"] == "Assembly"
    # A's sketch, A and its instance rebuilt; the model, B's sketch, B and its instance kept
    assert "model: 3 built, 4 unchanged" in stdout


def test_measure_with_a_printed_value_keeps_cae_in_its_module(monkeypatch):
    """Clicking Measure (or a Value it feeds) shows nothing in a viewport: CAE must not jump to Visualization."""
    monkeypatch.setattr(cae_queries, "value", lambda q, first, second=None: [[50.0, 25.0, 10.0], [50.0, 25.0, 0.0]])
    nodes = [("hole", "model.sketch", {"name": "Hole", "shape": "Circle", "x": 50, "y": 25, "radius": 10}),
             ("plate", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("r", "model.region", {"method": "By shape", "shape": "Circular edges", "create": "Set", "name": "Holes"}),
             ("m", "model.measure", {"quantity": "Center"}), ("v", "view.value", {})]
    edges = [("hole", "sketch", "plate", "add"), ("plate", "sketch", "p", "profile"),
             ("p", "part", "r", "source"), ("r", "region", "m", "first"), ("m", "value", "v", "value")]
    first, bad = script(nodes, edges, focus="r")
    assert not bad and first["module"] == "Part"
    for focus in ("m", "v"):
        again, _ = script(nodes, edges, previous=first, focus=focus)
        assert again["module"] == "", (focus, again["module"])


def test_removed_nodes_are_deleted_and_errors_keep_things(tmp_path):
    nodes, edges = chain()
    first, _ = script(nodes, edges)
    broken, bad = script(nodes[:2] + [("p", "model.part", {"body": "Solid"})] + nodes[3:], edges, previous=first)
    assert bad  # Part has an error, Instance is blocked: CAE keeps both until it's fixed
    gone, _ = script(nodes[:2], edges[:0], previous=broken)  # Part and Instance removed
    log, stdout = run_in_fake_cae([first["script"], broken["script"], gone["script"]], tmp_path)
    r1, r2, r3 = runs(log)
    assert not [c for c in r2 if c["call"].startswith("del ")]
    deleted = [(c["call"], c["args"]) for c in r3 if c["call"].startswith("del ")]
    assert deleted == [("del model[NoCode-Model].rootAssembly.instances", ["Part-1-1"]),
                       ("del model[NoCode-Model].parts", ["Part-1"])]


def test_two_editor_tabs_keep_their_own_models_in_one_cae(tmp_path):
    """Each tab (doc) deletes only what it built: syncing tab B doesn't remove tab A's model, and back on A nothing
    is rebuilt."""
    a_nodes, a_edges = chain(model={"name": "Bracket"})
    b_nodes, b_edges = chain(model={"name": "Plate"})
    a, _ = script(a_nodes, a_edges, doc="a", tab="model")
    b, _ = script(b_nodes, b_edges, doc="b", tab="model")
    a2, _ = script(a_nodes, a_edges, previous=a, doc="a", tab="model")
    log, stdout = run_in_fake_cae([a["script"], b["script"], a2["script"]], tmp_path)
    r1, r2, r3 = runs(log)
    # (the profile sketch a part is made from is temporary)
    assert not [c for c in r2 + r3 if c["call"].startswith("del ") and not c["args"][0].startswith("__nocode")]
    assert not calls(r3, "mdb.Model") and not calls(r3, ".Part")
    assert "model: 0 built, 4 unchanged, 0 deleted" in stdout


def test_one_model_name_in_two_tabs_is_shared_or_left_until_sync(tmp_path):
    """The same model open in two tabs is shared, not rebuilt by each (a running job syncs every few seconds). Two
    different graphs with one model name: automatic updates leave the other tab's model; Sync takes it over."""
    nodes, edges = chain(model={"name": "Mirror"})
    other = chain(model={"name": "Mirror"}, sketch={"shape": "Circle"})[0]
    a, _ = script(nodes, edges, doc="a", tab="model")
    same, _ = script(nodes, edges, doc="b", tab="model")
    live, _ = script(other, edges, doc="c", tab="model", take=False)
    sync, _ = script(other, edges, doc="c", tab="model")
    back, _ = script(nodes, edges, previous=a, doc="a", tab="model", take=False)
    log, stdout = run_in_fake_cae([s["script"] for s in (a, same, live, sync, back)], tmp_path)
    r1, r2, r3, r4, r5 = runs(log)
    assert not calls(r2, "mdb.Model") and not calls(r2, ".Part")  # shared
    said = stdout.splitlines()
    assert not calls(r3, ".Part") and any(ln.count("left as another editor tab built them:") and "part Mirror Part-1"
                                          in ln for ln in said), stdout
    assert calls(r4, ".Part") and any("another editor tab had built" in ln and "part Mirror Part-1" in ln
                                      for ln in said), stdout
    # back on the first tab, a live update leaves the part the other tab took over (no ping-pong)
    assert not calls(r5, ".Part") and not [c for c in r5 if c["call"].startswith("del model[Mirror].parts")]


def test_a_model_left_to_another_tab_is_not_measured(tmp_path):
    """A Live update that leaves the model to another tab (same name, different graph) would measure that tab's
    geometry and cache it as this graph's answer for good: it reports no measurements; Sync, which builds the model
    for this tab, does."""
    def measured(sketch):
        nodes, edges = chain(model={"name": "Mirror"}, sketch=sketch)
        return (nodes + [("mesh", "model.mesh", {}), ("n", "model.measure", {"quantity": "Nodes"})],
                edges + [("p", "part", "mesh", "target"), ("mesh", "part", "n", "first")])
    a, _ = script(*measured({}), doc="a", tab="model")
    live, _ = script(*measured({"shape": "Circle"}), doc="c", tab="model", take=False)
    sync, _ = script(*measured({"shape": "Circle"}), doc="c", tab="model")
    assert all(cae_queries.MARKER in s["script"] for s in (a, live, sync))
    log, stdout = run_in_fake_cae([s["script"] for s in (a, live, sync)], tmp_path)
    said = stdout.splitlines()
    left = next(i for i, ln in enumerate(said) if "left as another editor tab built them:" in ln)
    taken = next(i for i, ln in enumerate(said) if "another editor tab had built" in ln)
    answered = [i for i, ln in enumerate(said) if ln.startswith(cae_queries.MARKER)]
    assert len(answered) == 2 and answered[0] < left < taken < answered[1], stdout
    assert any("measured nothing" in ln for ln in said[left:taken]), stdout
    meshes = [i for i, ln in enumerate(said) if ln.startswith(cae_mesh.MARKER)]  # the preview's mesh: the same
    assert len(meshes) == 2 and meshes[0] < left < taken < meshes[1], stdout


def test_things_deleted_by_hand_in_cae_come_back(tmp_path):
    nodes, edges = chain()
    first, _ = script(nodes, edges)
    by_hand = "from abaqus import mdb\ndel mdb.models['NoCode-Model'].parts['Part-1']\n"
    log, _ = run_in_fake_cae([first["script"], by_hand, first["script"]], tmp_path)
    r3 = runs(log)[2]
    assert len(calls(r3, ".Part")) == 1 and len(calls(r3, ".Instance")) == 1 and not calls(r3, "mdb.Model")


def test_same_names_in_one_model_are_numbered(tmp_path):
    """Two Sketch nodes left at Sketch-1, two Parts at Part-1: CAE gets Sketch-1 and Sketch-1-2, Part-1 and Part-1-2."""
    nodes = [("s1", "model.sketch", {}), ("s2", "model.sketch", {"shape": "Circle"}),
             ("a", "model.part", {}), ("b", "model.part", {}), ("ia", "model.instance", {}), ("ib", "model.instance", {})]
    edges = [("s1", "sketch", "a", "profile"), ("s2", "sketch", "b", "profile"),
             ("a", "part", "ia", "part"), ("b", "part", "ib", "part")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    made = [c["kwargs"]["name"] for c in calls(log, ".ConstrainedSketch") if not c["kwargs"]["name"].startswith("__")]
    assert made == ["Sketch-1", "Sketch-1-2"]
    copies = [c["kwargs"] for c in calls(log, ".ConstrainedSketch") if c["kwargs"]["name"] == "__nocode_profile"]
    assert len(copies) == 2
    assert [c["kwargs"]["name"] for c in calls(log, ".Part")] == ["Part-1", "Part-1-2"]
    insts = calls(log, ".Instance")
    assert [(c["kwargs"]["name"], c["kwargs"]["part"]) for c in insts] == [
        ("Part-1-1", "<model[NoCode-Model].parts[Part-1]>"), ("Part-1-1-2", "<model[NoCode-Model].parts[Part-1-2]>")]
    assert "built as: Sketch-1 -> Sketch-1-2" in stdout
    # the second part really is made from the circle
    second = out["script"].split("def _make_")[[i for i, t in enumerate(out["script"].split("def _make_"))
                                                 if "'Part Part-1-2'" in t][0]]
    assert "m.sketches['Sketch-1-2']" in second


def test_instances_of_a_list_of_parts_and_placement(tmp_path):
    nodes = [("s", "model.sketch", {}), ("a", "model.part", {"name": "A"}), ("b", "model.part", {"name": "B"}),
             ("i", "model.instance", {"z": 40, "axis": "X", "angle": 90})]  # one sketch, two parts
    edges = [("s", "sketch", "a", "profile"), ("s", "sketch", "b", "profile")]
    # a list of parts into Instance: one instance each
    nodes.insert(3, ("l", "list.merge", {}))
    edges = edges + [("a", "part", "l", "a"), ("b", "part", "l", "b"), ("l", "list", "i", "part")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    log, _ = run_in_fake_cae(out["script"], tmp_path)
    assert [c["kwargs"]["name"] for c in calls(log, ".Instance")] == ["A-1", "B-1"]
    assert calls(log, "rootAssembly.rotate")[0]["kwargs"]["axisDirection"] == [1.0, 0.0, 0.0]
    assert calls(log, "rootAssembly.translate")[0]["kwargs"]["vector"] == [0.0, 0.0, 40.0]


def test_instance_remove_accepts_a_filtered_list_and_deletes_only_those_instances(tmp_path):
    base_nodes = [("s", "model.sketch", {}), ("p", "model.part", {}),
                  ("a", "model.instance", {"name": "A"}), ("b", "model.instance", {"name": "B"})]
    base_edges = [("s", "sketch", "p", "profile"), ("p", "part", "a", "part"),
                  ("p", "part", "b", "part")]
    first, bad = script(base_nodes, base_edges)
    assert not bad, bad
    nodes = base_nodes + [("all", "list.merge", {}),
                          ("yes", "input.value", {"type": "Yes/No", "value": "Yes"}),
                          ("no", "input.value", {"type": "Yes/No", "value": "No"}),
                          ("mask", "list.merge", {}),
                          ("pick", "list.filter", {}), ("remove", "model.instance", {"action": "Remove"})]
    edges = base_edges + [("a", "instance", "all", "a"), ("b", "instance", "all", "b"),
                          ("yes", "value", "mask", "a"), ("no", "value", "mask", "b"),
                          ("all", "list", "pick", "list"), ("mask", "list", "pick", "keep"),
                          ("pick", "list", "remove", "part")]
    second, bad = script(nodes, edges, previous=first)
    assert not bad, bad
    log, stdout = run_in_fake_cae([first["script"], second["script"]], tmp_path)
    assert "failed" not in stdout, stdout
    r1, r2 = runs(log)
    assert [c["kwargs"]["name"] for c in calls(r1, ".Instance")] == ["A", "B"]
    assert [c["args"] for c in r2 if c["call"].startswith("del ") and c["call"].endswith("instances")] == [["A"]]
    assert not calls(r2, ".Instance")  # B stays unchanged; A is removed, not rebuilt


def test_clicked_node_is_shown(tmp_path):
    nodes, edges = chain()
    for focus, module, shown in (("p", "Part", "parts[Part-1]"), ("m", "Assembly", "rootAssembly")):
        out, _ = script(nodes, edges, focus=focus)
        assert out["module"] == module
        log, stdout = run_in_fake_cae(out["script"], tmp_path)
        objs = [c["kwargs"]["displayedObject"] for c in calls(log, ".setValues") if "displayedObject" in c["kwargs"]]
        assert objs and objs[-1].endswith(shown + ">"), objs
        assert "failed" not in stdout, stdout


def test_clicked_sketch_is_shown_in_the_sketch_module(tmp_path):
    nodes, edges = chain()
    out, _ = script(nodes, edges, focus="s")
    assert out["module"] == "Sketch"
    # displayed as picking it in CAE's Sketch combo does (an edit copy left the Sketch module empty)
    assert "vp.setValues(displayedObject=mdb.models['NoCode-Model'].sketches['Sketch-1'])" in out["script"]
    assert "setPrimaryObject" not in out["script"] and "__edit__" not in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    objs = [c["kwargs"]["displayedObject"] for c in calls(log, ".setValues") if "displayedObject" in c["kwargs"]]
    assert objs and objs[-1].endswith("sketches[Sketch-1]>"), objs


def test_clicked_input_picked_in_cae_is_shown_in_its_module(tmp_path):
    for kind, value, module, shown in (
            ("Sketch in CAE", "Model-1|Profile", "Sketch", "displayedObject=mdb.models['Model-1'].sketches['Profile']"),
            ("Part in CAE", "Model-1|Bracket", "Part", "mdb.models['Model-1'].parts['Bracket']"),
            ("Instance in CAE", "Model-1|Bracket-1|Bracket", "Assembly", "mdb.models['Model-1'].rootAssembly")):
        # on its own, and wired into the modelling (the Part copies a picked sketch or part)
        for wired in (False, True):
            nodes = [("in", "input.value", {"type": kind, "value": value})]
            edges = []
            if wired and kind != "Instance in CAE":
                nodes.append(("p", "model.part", {"name": "Copy"}))
                edges.append(("in", "value", "p", "profile" if kind == "Sketch in CAE" else "model"))
            if wired and kind == "Part in CAE":
                nodes[1] = ("p", "model.instance", {})
                edges = [("in", "value", "p", "part")]
            out, bad = script(nodes, edges, focus="in")
            assert not bad, bad
            assert out["module"] == module, (kind, out["module"])
            assert shown in out["script"], (kind, wired)
            # the user's model, already in CAE
            seed = ("from abaqus import mdb\nfrom abaqusConstants import *\nm = mdb.Model(name='Model-1')\n"
                    "m.ConstrainedSketch(name='Profile', sheetSize=200.0)\n"
                    "p = m.Part(name='Bracket', dimensionality=THREE_D, type=DEFORMABLE_BODY)\n"
                    "m.rootAssembly.Instance(name='Bracket-1', part=p, dependent=ON)\n")
            log, stdout = run_in_fake_cae([seed, out["script"]], tmp_path)
            assert "failed" not in stdout and "Traceback" not in stdout, stdout


def test_clicked_node_goes_to_its_module(tmp_path):
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("sec", "model.section", {}), ("mesh", "model.mesh", {}), ("i", "model.instance", {}),
             ("face", "model.region", {"kind": "Face", "value": "0"}), ("step", "model.step", {"name": "Pull"}),
             ("bc", "model.condition", {"kind": "Fixed"}),
             ("tie", "model.interaction", {"kind": "Kinematic coupling"}), ("job", "model.job", {})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"), ("sec", "part", "mesh", "target"),
             ("mesh", "part", "i", "part"), ("i", "instance", "face", "source"), ("step", "step", "bc", "step"),
             ("face", "region", "bc", "region"), ("face", "region", "tie", "slave"),
             ("bc", "condition", "job", "analysis")]
    expected = {"sec": ("Property", "parts[Part-1]", "partDisplay.setValues(sectionAssignments=ON)"),
                "mesh": ("Mesh", "parts[Part-1]", "partDisplay.setValues(mesh=ON)"),
                "step": ("Step", "rootAssembly", "assemblyDisplay.setValues(step='Pull')"),
                "bc": ("Load", "rootAssembly", "assemblyDisplay.setValues(loads=ON, bcs=ON, fields=ON)"),
                "tie": ("Interaction", "rootAssembly", "assemblyDisplay.setValues(step='Initial')"),
                "job": ("Job", "rootAssembly", "")}
    for focus, (module, shown, display) in expected.items():
        out, bad = script(nodes, edges, focus=focus)
        assert not bad, bad
        assert out["module"] == module, (focus, out["module"])
        assert display in out["script"], focus
        log, stdout = run_in_fake_cae(out["script"], tmp_path)
        assert "failed" not in stdout, stdout
        objs = [c["kwargs"]["displayedObject"] for c in calls(log, ".setValues") if "displayedObject" in c["kwargs"]]
        assert objs[-1].endswith(shown + ">"), (focus, objs)
    # an independent instance has its own mesh: the Mesh module shows the assembly
    nodes[4] = ("i", "model.instance", {"dependency": "Independent"})
    out, bad = script(nodes, edges, focus="mesh")
    assert not bad, bad
    assert "assemblyDisplay.setValues(mesh=ON)" in out["script"] and out["module"] == "Mesh"


def test_clicked_region_is_shown_and_highlighted(tmp_path):
    nodes, edges = chain()
    nodes.append(("r", "model.region", {"kind": "Face", "value": "0, 2"}))
    edges.append(("p", "part", "r", "source"))
    out, bad = script(nodes, edges, focus="r")
    assert not bad, bad
    assert "unhighlight(object=_old)" in out["script"]
    assert "highlight(object=_selected)" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, "highlight")


def test_clicked_list_of_regions_highlights_them_all(tmp_path):
    """A Region node giving several regions (a list into its Selection) highlights every one, not only the first;
    on the part when they all are on it, else on the assembly (a part's Region on each instance of the part)."""
    nodes, edges = chain()
    nodes += [("sel", "input.value", {"type": "List / matrix", "value": '["0", "2"]'}),
              ("r", "model.region", {"kind": "Face"})]
    edges += [("p", "part", "r", "source"), ("sel", "value", "r", "param:value")]
    out, bad = script(nodes, edges, focus="r")
    assert not bad, bad
    assert "Face Region 2 of 2" in out["script"] and out["module"] == "Part"
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert len(calls(log, "highlight")) == 2
    # one Region on the part and one on its instance: both shown on the assembly
    nodes, edges = chain()
    nodes += [("both", "list.merge", {}), ("r", "model.region", {"kind": "Face", "value": "0"})]
    edges += [("p", "part", "both", "a"), ("i", "instance", "both", "b"), ("both", "list", "r", "source")]
    out, bad = script(nodes, edges, focus="r")
    assert not bad, bad
    assert out["module"] == "Assembly" and "_on_instances(" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert len(calls(log, "highlight")) == 2
    # a general node passing Regions on (Merge here; Pick from List, Compare…): clicked, it shows all of them
    nodes, edges = chain()
    nodes += [("r1", "model.region", {"kind": "Face", "value": "0"}),
              ("r2", "model.region", {"kind": "Face", "value": "2"}), ("both", "list.merge", {})]
    edges += [("p", "part", "r1", "source"), ("p", "part", "r2", "source"),
              ("r1", "region", "both", "a"), ("r2", "region", "both", "b")]
    out, bad = script(nodes, edges, focus="both")
    assert not bad, bad
    assert "'Show Merge'" in out["script"] and out["module"] == "Part"
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert len(calls(log, "highlight")) == 2


def test_modelling_tab_without_nodes_clears_cae(tmp_path):
    nodes, edges = chain()
    first, _ = script(nodes, edges)
    empty = build_script({"nodes": [], "edges": [], "tab": "model"}, {}, {})
    log, _ = run_in_fake_cae([first["script"], empty["script"]], tmp_path)
    assert calls(runs(log)[1], "del mdb.models")


def test_auto_launch_picks_cae_for_models(monkeypatch):
    from nodecode_fea import app as app_module
    seen = {}
    monkeypatch.setattr(app_module.link, "launch",
                        lambda script, product="cae", module="Visualization", sketch=None: seen.update(p=product, m=module) or {})
    TestClient(app_module.app).post("/api/cae/launch", json={"graph": graph(*chain())})
    assert seen == {"p": "cae", "m": "Assembly"}


# ------------------------------------------------------------------ lists flow through modelling

def heights(values):
    return ("h", "input.value", {"type": "List / matrix", "value": str(values)})


def test_a_list_of_heights_makes_a_sketch_part_and_instance_each(tmp_path):
    nodes, edges = chain()
    nodes = nodes + [heights([10, 20, 30])]
    edges = edges + [("h", "value", "s", "param:height")]
    g, res, inputs = evaluate(nodes, edges)
    assert res["s"]["summary"].startswith("3") and res["p"]["status"] == "ok", res
    parts = inputs.outputs["p"]["part"]
    assert [p.name for p in parts] == ["Part-1-1", "Part-1-2", "Part-1-3"]
    assert [p.sketch.name for p in parts] == ["Sketch-1-1", "Sketch-1-2", "Sketch-1-3"]
    assert [i.name for i in inputs.outputs["i"]["instance"]] == ["Part-1-1-1", "Part-1-2-1", "Part-1-3-1"]
    out = build_script(g, res, inputs)
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert len(calls(log, ".Part")) == 3 and len(calls(log, ".Instance")) == 3, stdout


def test_two_lists_pair_up_or_make_every_combination():
    nodes = [("s", "model.sketch", {}), ("w", "input.value", {"type": "List / matrix", "value": "[100, 200, 300]"}),
             heights([10, 20, 30, 40]), ("p", "model.part", {})]
    edges = [("w", "value", "s", "param:width"), ("h", "value", "s", "param:height"), ("s", "sketch", "p", "profile")]
    _, res, _ = evaluate(nodes, edges)
    assert res["s"]["status"] == "error" and "All combinations" in res["s"]["message"]
    g = graph(nodes, edges)
    g["nodes"][0]["combine"] = "all"
    res, inputs = Engine().evaluate(g)
    assert res["s"]["summary"] == "3 × 4 items" and res["p"]["summary"] == "3 × 4 items", res
    grid = inputs.outputs["p"]["part"]
    assert grid[1][2].name == "Part-1-2-3" and grid[1][2].sketch.items[0][2] == (200.0, 30.0)


def test_same_part_placed_by_a_list_of_positions_and_names_from_a_list():
    nodes, edges = chain()
    nodes = nodes + [("x", "input.value", {"type": "List / matrix", "value": "[0, 100, 200]"}),
                     ("n", "input.value", {"type": "List / matrix", "value": '["Left", "Mid", "Right"]'})]
    _, res, inputs = evaluate(nodes, edges + [("x", "value", "i", "param:x")])
    assert [i.name for i in inputs.outputs["i"]["instance"]] == ["Part-1-1", "Part-1-2", "Part-1-3"]
    _, res, inputs = evaluate(nodes, edges + [("x", "value", "i", "param:x"), ("n", "value", "i", "param:name")])
    assert [i.name for i in inputs.outputs["i"]["instance"]] == ["Left", "Mid", "Right"], res["i"]


def test_points_table_is_one_profile_and_a_list_of_tables_is_many():
    one = ("pts", "input.value", {"type": "List / matrix", "value": "[[0, 0], [100, 0], [50, 80]]"})
    many = ("pts", "input.value", {"type": "List / matrix",
                                   "value": "[[[0, 0], [100, 0], [50, 80]], [[0, 0], [10, 0], [5, 8]]]"})
    for src, count in ((one, 1), (many, 2)):
        _, res, inputs = evaluate([src, ("s", "model.sketch", {"shape": "Profile", "closed": "Closed"})],
                                  [("pts", "value", "s", "points")])
        out = inputs.outputs["s"]["sketch"]
        assert (len(out) if isinstance(out, list) else 1) == count, res["s"]


def test_merge_flattens_a_grid():
    nodes = [("g", "input.value", {"type": "List / matrix", "value": "[[1, 2, 3], [4, 5]]"}),
             ("m", "list.merge", {"levels": "Flatten"}), ("k", "list.merge", {})]
    _, res, inputs = evaluate(nodes, [("g", "value", "m", "a"), ("g", "value", "k", "a")])
    assert [n.value for n in inputs.outputs["m"]["list"]] == [1, 2, 3, 4, 5]
    assert len(inputs.outputs["k"]["list"]) == 2


def test_sketch_model_decides_where_sketch_and_part_go(tmp_path):
    a, b = ("ma", "model.model", {"name": "A"}), ("mb", "model.model", {"name": "B"})
    sk, part = ("s", "model.sketch", {}), ("p", "model.part", {})
    # Model into Sketch only: the sketch and the part made from it are both in A
    out, bad = script([a, sk, part], [("ma", "model", "s", "model"), ("s", "sketch", "p", "profile")])
    assert not bad, bad
    log, _ = run_in_fake_cae(out["script"], tmp_path)
    assert [c["kwargs"]["name"] for c in calls(log, "mdb.Model")] == ["A"]
    assert calls(log, "model[A].Part") and calls(log, "model[A].ConstrainedSketch")
    # Part wired to B: the part goes to B, the sketch is in A (its own) and in B (for the part)
    out, bad = script([a, b, sk, part], [("ma", "model", "s", "model"), ("mb", "model", "p", "model"),
                                         ("s", "sketch", "p", "profile")])
    assert not bad, bad
    log, _ = run_in_fake_cae(out["script"], tmp_path)
    assert calls(log, "model[B].Part") and not calls(log, "model[A].Part")
    made = {c["call"].split(".")[0] for c in calls(log, ".ConstrainedSketch") if c["kwargs"]["name"] == "Sketch-1"}
    assert made == {"model[A]", "model[B]"}, made
    # nothing wired: the graph's Model (not NoCode-Model beside it); with several, the one the editor names
    # (selected last), else the first; no Model node at all: NoCode-Model
    for nodes, extra, home in (([b, sk, part], {}, "B"), ([a, b, sk, part], {}, "A"),
                               ([a, b, sk, part], {"model": "mb"}, "B"), ([sk, part], {}, "NoCode-Model")):
        out, bad = script(nodes, [("s", "sketch", "p", "profile")], **extra)
        assert not bad, bad
        log, _ = run_in_fake_cae(out["script"], tmp_path)
        assert {c["call"].split(".")[0] for c in calls(log, ".ConstrainedSketch")} == {f"model[{home}]"}, home
        assert calls(log, f"model[{home}].Part"), home
    # a first Step, unwired, too
    _, res, inputs = evaluate([b, ("st", "model.step", {})], [])
    assert inputs.outputs["st"]["step"].model.name == "B"
    # no Model node, work on a part chosen from CAE: its model (not NoCode-Model beside it)
    _, res, inputs = evaluate([("in", "input.value", {"type": "Part in CAE", "value": "Hand Model|Plate"}),
                               ("st", "model.step", {})], [])
    assert inputs.outputs["st"]["step"].model.name == "Hand Model"


def test_infinite_numbers_are_an_error_on_the_node():
    nodes, edges = chain()  # rotations 180 / i over a list: the first one is 180 / 0
    _, res, _ = evaluate(nodes + [("r", "math.formula", {"expr": "180 / np.arange(a)"}),
                                  ("one", "input.value", {"value": "3"})],
                         edges + [("one", "value", "r", "a"), ("r", "value", "i", "param:angle")])
    assert res["i"]["status"] == "error" and "Rotation is infinite" in res["i"]["message"], res["i"]


def test_one_broken_node_does_not_freeze_the_rest(tmp_path):
    # two parts, each placed by its own Instance
    nodes = [("s", "model.sketch", {}), ("a", "model.part", {"name": "A"}), ("b", "model.part", {"name": "B"}),
             ("ia", "model.instance", {}), ("ib", "model.instance", {})]
    edges = [("s", "sketch", "a", "profile"), ("s", "sketch", "b", "profile"),
             ("a", "part", "ia", "part"), ("b", "part", "ib", "part")]
    first, _ = script(nodes, edges)
    # Instance A breaks (an infinite rotation), Instance B is turned: B is rebuilt, A's instance is kept
    bad_a = ("r", "math.formula", {"expr": "1 / (a * 0)"})  # a division by zero: Instance A waits on it
    one = ("one", "input.value", {})
    broken_nodes = nodes[:4] + [("ib", "model.instance", {"angle": 30}), bad_a, one]
    broken_edges = edges + [("one", "value", "r", "a"), ("r", "value", "ia", "param:angle")]
    second, bad = script(broken_nodes, broken_edges, previous=first)
    assert "ia" in bad and "ib" not in bad
    # then Instance B is deleted while A is still broken: B's instance goes, A's stays
    third, _ = script(broken_nodes[:4] + broken_nodes[5:], [e for e in broken_edges if "ib" not in e],
                      previous=second)
    log, stdout = run_in_fake_cae([first["script"], second["script"], third["script"]], tmp_path)
    r1, r2, r3 = runs(log)
    assert [c["kwargs"]["angle"] for c in calls(r2, "rootAssembly.rotate")] == [30.0]
    assert not [c for c in r2 if c["call"].endswith("instances") and c["args"] == ["A-1"]]
    deleted = [c["args"] for c in r3 if c["call"].startswith("del ") and c["call"].endswith("instances")]
    assert deleted == [["B-1"]], deleted
    assert "kept as they were until fixed (error in the editor): Instance" in stdout


def test_x_y_move_a_profile():
    _, res, inputs = evaluate([("s", "model.sketch", {"shape": "Profile", "points": "0,0; 10,0; 10,5", "x": 100,
                                                      "y": -5})], [])
    assert inputs.outputs["s"]["sketch"].items[0][1] == ((100.0, -5.0), (110.0, -5.0), (110.0, 0.0))


def test_every_when_names_a_real_choice():
    from nodecode_fea.nodes import REGISTRY
    for d in REGISTRY.values():
        params = {p.name: p for p in d.params}
        for item in [*d.params, *d.inputs]:
            for k, values in (item.when or {}).items():
                if k.startswith(("unwired:", "wired:")):  # a typed fallback for an input port / set once wired
                    assert any(port.name == k.split(":", 1)[1] for port in d.inputs), (d.type, item.name, k)
                    continue
                if k in params and params[k].kind == "pick":  # shown while nothing is chosen from CAE's list
                    assert values == [""], (d.type, item.name, values)
                    continue
                assert k in params and params[k].kind == "select", (d.type, item.name, k)
                assert set(values) <= set(params[k].options), (d.type, item.name, values)


# ------------------------------------------------------------------ chains of Instance nodes

def chained(i2=None, extra=()):
    """Sketch -> Part -> Instance (Z 30) -> Instance (X 90 about a pivot) -> Instance (move z 300)."""
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {}),
             ("i1", "model.instance", {"angle": 30}),
             ("i2", "model.instance", i2 or {"axis": "X", "angle": 90, "pivot": "10, 0, 0"}),
             ("i3", "model.instance", {"z": 300}), *extra]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "i1", "part"), ("i1", "instance", "i2", "part"),
             ("i2", "instance", "i3", "part")]
    return nodes, edges


def test_a_chain_of_instances_is_one_instance_with_every_step(tmp_path):
    nodes, edges = chained()
    out, bad = script(nodes, edges)
    assert not bad, bad
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert [c["kwargs"]["name"] for c in calls(log, ".Instance")] == ["Part-1-1"]  # no copies
    steps = [(c["call"].split(".")[-1], c["kwargs"]) for c in log
             if c["call"].endswith(("rootAssembly.rotate", "rootAssembly.translate"))]
    assert steps == [("rotate", {"instanceList": ["Part-1-1"], "axisPoint": [0.0, 0.0, 0.0],
                                 "axisDirection": [0.0, 0.0, 1.0], "angle": 30.0}),
                     ("rotate", {"instanceList": ["Part-1-1"], "axisPoint": [10.0, 0.0, 0.0],
                                 "axisDirection": [1.0, 0.0, 0.0], "angle": 90.0}),
                     ("translate", {"instanceList": ["Part-1-1"], "vector": [0.0, 0.0, 300.0]})]
    # changing the first rotation rebuilds that one instance (not a second one)
    nodes[2] = ("i1", "model.instance", {"angle": 45})
    again, _ = script(nodes, edges, previous=out)
    log, stdout = run_in_fake_cae([out["script"], again["script"]], tmp_path)
    r2 = runs(log)[1]
    assert [c["kwargs"]["name"] for c in calls(r2, ".Instance")] == ["Part-1-1"]
    assert "1 built" in stdout.split("model:")[-1]


def test_a_list_on_a_later_step_makes_numbered_copies():
    nodes, edges = chained(extra=[("ang", "input.value", {"type": "List / matrix", "value": "[0, 90, 180]"})])
    g, res, inputs = evaluate(nodes, edges + [("ang", "value", "i2", "param:angle")])
    out = build_script(g, res, inputs)
    made = [ln for ln in out["script"].splitlines() if "a.Instance(" in ln]
    assert len(made) == 3 and all("Part-1-1-" in ln for ln in made), made
    insts = inputs.outputs["i3"]["instance"]
    assert [len(i.steps) for i in insts] == [2, 3, 3]  # 0° adds no rotation


def test_a_chain_through_a_list_node_still_builds_only_its_end():
    nodes, edges = chained()
    nodes = nodes[:3] + [("l", "list.merge", {})] + nodes[3:4]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "i1", "part"), ("i1", "instance", "l", "a"),
             ("l", "list", "i2", "part")]
    g, res, inputs = evaluate(nodes, edges)
    out = build_script(g, res, inputs)
    assert sum("a.Instance(" in ln for ln in out["script"].splitlines()) == 1


@pytest.mark.parametrize("pivot", ["1, 2, 3; 4, 5, 6", "a, b, c"])
def test_pivot_is_one_point(pivot):
    _, res, _ = evaluate(*chain(inst={"angle": 10, "pivot": pivot}))
    assert res["i"]["status"] == "error", res["i"]


def test_step_output_requests_are_their_own_cae_object(tmp_path):
    from nodecode_fea.cae_model import collect

    def objects(field, history="", region=False):
        nodes = [("m", "model.model", {}), ("s", "model.sketch", {}),
                 ("p", "model.part", {"body": "Solid", "method": "Extrude"}), ("i", "model.instance", {}),
                 ("r", "model.region", {"kind": "Face", "value": "0"}),
                 ("st", "model.step", {"name": "Load", "field": field, "intervals": 4, "history": history}),
                 ("bc", "model.condition", {"kind": "Fixed"})]
        edges = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "i", "part"),
                 ("i", "instance", "r", "source"), ("m", "model", "st", "after"), ("st", "step", "bc", "step"),
                 ("r", "region", "bc", "region")] + ([("r", "region", "st", "history_region")] if region else [])
        g, res, inputs = evaluate(nodes, edges)
        assert all(v["status"] == "ok" for v in res.values()), res
        return {o["key"]: o for o in collect(g, res, inputs)[0]}, build_script(g, res, inputs)["script"]

    a, _ = objects("U, RF")
    b, text = objects("u, rf, peeq", "RF1", region=True)
    # changing what is written doesn't rebuild the step or the loads in it
    for key in ("step|NoCode-Model|Load", "condition|NoCode-Model|bc|Condition-1"):
        assert a[key]["fp"] == b[key]["fp"]
    assert a["output|NoCode-Model|Load"]["fp"] != b["output|NoCode-Model|Load"]["fp"]
    assert "setValuesInStep(stepName='Load', variables=('U', 'RF', 'PEEQ'), numIntervals=4)" in text
    assert "HistoryOutputRequest(name='Load-History', createStepName='Load', variables=('RF1',), frequency=1, " \
           "region=history_region)" in text
    log, stdout = run_in_fake_cae(text, tmp_path)
    assert "failed" not in stdout, stdout


def test_step_output_request_errors():
    def run(params, region_on_part=False):
        nodes = [("s", "model.sketch", {}), ("p", "model.part", {}), ("r", "model.region", {"kind": "Face", "value": "0"}),
                 ("st", "model.step", params)]
        edges = [("s", "sketch", "p", "profile"), ("p", "part", "r", "source")]
        edges += [("r", "region", "st", "history_region")] if region_on_part else []
        return evaluate(nodes, edges)[1]["st"]
    assert "isn't an Abaqus output variable" in run({"field": "S, U-2"})["message"]
    assert "Region on an Instance" in run({"history": "RF2"}, region_on_part=True)["message"]
    assert run({})["status"] == "ok"


def test_material_plastic_table_from_text_or_a_curve(tmp_path):
    base = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "sec", "target")]
    out, bad = script(base + [("sec", "model.section", {"plastic": "0, 250; 0.1, 350"})], edges)
    assert not bad, bad
    assert "material.Plastic(table=((250.0, 0.0), (350.0, 0.1),))" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    # a curve (x = plastic strain, y = stress) wired in, e.g. from test data
    curve = [("ys", "input.value", {"type": "List / matrix", "value": "[200, 260, 300]"}),
             ("xs", "input.value", {"type": "List / matrix", "value": "[0, 0.05, 0.2]"}),
             ("c", "series.make", {})]
    out, bad = script(base + curve + [("sec", "model.section", {})],
                      edges + [("ys", "value", "c", "y"), ("xs", "value", "c", "x"), ("c", "series", "sec", "plastic")])
    assert not bad, bad
    assert "material.Plastic(table=((200.0, 0.0), (260.0, 0.05), (300.0, 0.2),))" in out["script"]
    for text, message in (("0.01, 250; 0.1, 300", "starts at 0 plastic strain"),
                          ("0, 250; 0, 300", "must increase"), ("0, -5", "must be positive")):
        _, bad = script(base + [("sec", "model.section", {"plastic": text})], edges)
        assert message in bad["sec"], bad
    out, bad = script(base + [("sec", "model.section", {"plastic": "0, 250"})], edges)  # perfectly plastic
    assert not bad and "material.Plastic(table=((250.0, 0.0),))" in out["script"]


def test_condition_amplitude_and_distribution_build_in_cae(tmp_path):
    base = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
            ("face", "model.region", {"kind": "Face", "value": "0"}), ("step", "model.step", {"name": "Loading"}),
            ("i", "model.instance", {})]
    wires = [("s", "sketch", "p", "profile"), ("p", "part", "face", "source"), ("step", "step", "push", "step"),
             ("face", "region", "push", "region"), ("push", "condition", "move", "step"),
             ("face", "region", "move", "region"), ("p", "part", "i", "part")]
    nodes = base + [("push", "model.condition", {"name": "Push", "kind": "Pressure", "magnitude": 2,
                                                 "amplitude": "0, 0; 0.5, 1; 1, 0.2",
                                                 "distribution": "1 + 0.1 * X + sin(pi * Y / 20)"}),
                    ("move", "model.condition", {"name": "Move", "kind": "Displacement / rotation", "u3": "1",
                                                 "amplitude": "0, 0; 1, 1", "amplitude_shape": "Smooth step",
                                                 "distribution": "0, 0, 0, 1; 10, 0, 0, 2"})]
    out, bad = script(nodes, wires)
    assert not bad, bad
    code = out["script"]
    assert "m.TabularAmplitude(name='Push-Amp', timeSpan=STEP" in code and "(0.5, 1.0)" in code
    assert "m.ExpressionField(name='Push-Field'" in code and "1 + 0.1 * X + sin(pi * Y / 20)" in code
    assert "distributionType=FIELD, field='Push-Field', magnitude=2.0, amplitude='Push-Amp'" in code
    assert "m.SmoothStepAmplitude(name='Move-Amp'" in code and "m.MappedField(name='Move-Field'" in code
    assert "distributionType=FIELD, fieldName='Move-Field', amplitude='Move-Amp'" in code
    log, stdout = run_in_fake_cae(code, tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, ".TabularAmplitude") and calls(log, ".MappedField") and calls(log, ".ExpressionField")

    # Wired: a result field from another analysis becomes mapped points; a curve becomes the amplitude.
    odb = [("odb", "odb.open", {"path": "mock://cantilever?nx=4&ny=1&nz=1"}), ("u", "odb.field", {"variable": "U"}),
           ("u2", "field.component", {"component": "U2"}),
           ("curve", "input.value", {"type": "List / matrix", "value": "[[0, 0], [2, 1]]"})]
    _, res, inputs = evaluate(base + odb + [("push", "model.condition", {"kind": "Pressure"})],
                              wires[:4] + [("odb", "odb", "u", "odb"), ("u", "field", "u2", "field"),
                                           ("u2", "field", "push", "distribution"),
                                           ("curve", "value", "push", "amplitude")])
    assert res["push"]["status"] == "ok", {k: v["message"] for k, v in res.items() if v["status"] != "ok"}
    cond = inputs.outputs["push"]["condition"]
    assert cond.amplitude == ((0.0, 0.0), (2.0, 1.0))
    assert cond.distribution[0] == "points" and len(cond.distribution[1][0]) == 4 and len(cond.distribution[1]) > 4
    assert "over time varying in space" in cond.summary()

    for setting, message in (({"amplitude": "1, 0; 0, 1"}, "never go back"),
                             ({"distribution": "1 + T"}, "unknown name 'T'"),
                             ({"distribution": "1, 2, 3"}, "x, y, z, value rows")):
        _, res, _ = evaluate(base + [("push", "model.condition", dict(kind="Pressure", **setting))], wires[:4])
        assert res["push"]["status"] == "error" and message in res["push"]["message"], (setting, res["push"])


def test_coupling_drives_a_face_from_a_reference_point_that_takes_loads(tmp_path):
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("i", "model.instance", {}), ("face", "model.region", {"kind": "Face", "value": "0"}),
             ("fix", "model.region", {"kind": "Face", "value": "1"}),
             ("step", "model.step", {"name": "Pull", "history": "RF1, U1"}),
             ("rbe", "model.interaction", {"name": "Grip", "kind": "Kinematic coupling", "point": "50, 5, 5",
                                           "dofs": "U1, U2, U3"}),
             ("hold", "model.condition", {"name": "Hold", "kind": "Fixed"}),
             ("pull", "model.condition", {"name": "Pull-force", "kind": "Concentrated force", "x": 100})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "i", "part"), ("i", "instance", "face", "source"),
             ("i", "instance", "fix", "source"), ("face", "region", "rbe", "slave"),
             ("step", "step", "hold", "step"), ("fix", "region", "hold", "region"),
             ("hold", "condition", "pull", "step"), ("rbe", "interaction", "pull", "region"),
             ("rbe", "interaction", "step", "history_region")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    code = out["script"]
    assert "a.ReferencePoint(point=(50.0, 5.0, 5.0))" in code
    assert "a.Set(name='Grip-RP', referencePoints=(a.referencePoints[_rp.id],))" in code
    assert ("m.Coupling(name='Grip', controlPoint=a.sets['Grip-RP'], surface=coupled_surface, "
            "influenceRadius=WHOLE_SURFACE, couplingType=KINEMATIC, localCsys=None, u1=ON, u2=ON, u3=ON, "
            "ur1=OFF, ur2=OFF, ur3=OFF)") in code
    assert "target_region = mdb.models['NoCode-Model'].rootAssembly.sets['Grip-RP']" in code
    assert "history_region = mdb.models['NoCode-Model'].rootAssembly.sets['Grip-RP']" in code
    log, stdout = run_in_fake_cae(code, tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, ".Coupling") and calls(log, ".ReferencePoint") and calls(log, ".ConcentratedForce")

    for change, message in (({"kind": "Pressure"}, "a coupling's reference point takes forces"),):
        bad_nodes = nodes[:-1] + [("pull", "model.condition", dict({"name": "P"}, **change))]
        _, res, _ = evaluate(bad_nodes, edges)
        assert res["pull"]["status"] == "error" and message in res["pull"]["message"], res["pull"]
    bad_nodes = nodes[:6] + [("rbe", "model.interaction", {"kind": "Distributing coupling", "dofs": "U7"})] + nodes[7:]
    _, res, _ = evaluate(bad_nodes, edges)
    assert res["rbe"]["status"] == "error" and "unknown 'U7'" in res["rbe"]["message"]
    bad_nodes = nodes[:6] + [("rbe", "model.interaction", {"kind": "Kinematic coupling", "influence": -1})] + nodes[7:]
    _, res, _ = evaluate(bad_nodes, edges)
    assert res["rbe"]["status"] == "error" and "cannot be negative" in res["rbe"]["message"]

    # a radius of influence couples only the nodes near the point
    near = nodes[:6] + [("rbe", "model.interaction", dict(nodes[6][2], influence=2.5))] + nodes[7:]
    out, bad = script(near, edges)
    assert not bad, bad
    assert "influenceRadius=2.5, couplingType=KINEMATIC" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout


def test_material_and_section_use_ones_already_in_cae(tmp_path):
    # the user keeps a Steel material and a Plate section in a library model; the graph's model copies them in
    seed = ("from abaqus import mdb\nm = mdb.Model(name='Lib')\nm.Material(name='Steel')\n"
            "m.HomogeneousSolidSection(name='Plate', material='Steel', thickness=None)\n")
    base = [("m", "model.model", {"name": "Work"}), ("s", "model.sketch", {}),
            ("p", "model.part", {"body": "Solid", "method": "Extrude"}), ("sec", "model.section", {"section": "Own"})]
    edges = [("m", "model", "p", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"),
             ("pick", "value", "sec", "existing")]
    for kind, value in (("Material in CAE", "Lib|Steel"), ("Section in CAE", "Lib|Plate")):
        out, bad = script(base + [("pick", "input.value", {"type": kind, "value": value})], edges)
        assert not bad, bad
        text = out["script"]
        assert "m.Material(name=" not in text  # its properties stay CAE's
        if kind == "Material in CAE":  # copied, and this node's section is of it
            assert "m.copyMaterials(sourceModel=_source, names=('Steel',))" in text
            assert "HomogeneousSolidSection(name='Own', material='Steel'" in text
            assigned = "Own"
        else:  # copied (with its material) and assigned as it is
            assert "m.copySections(sourceModel=_source, names=('Plate',))" in text
            assert "HomogeneousSolidSection" not in text
            assigned = "Plate"
        assert "_source = mdb.models['Lib']" in text and f"sectionName={assigned!r}" in text
        log, stdout = run_in_fake_cae([seed, text], tmp_path)
        assert "failed" not in stdout, stdout
        assert not [c for c in log if c["call"].startswith("del ") and "Lib" in c["call"]]  # the library is only read
    # from the model the graph itself rebuilds: refused (the rebuild would delete it)
    out, bad = script(base + [("pick", "input.value", {"type": "Material in CAE", "value": "Work|Steel"})], edges)
    assert "rebuilds model Work" in bad["sec"]
    # anything else into it says what it takes
    out, bad = script(base + [("pick", "input.value", {"type": "Number", "value": "3"})], edges)
    assert "Material in CAE or Section in CAE" in bad["sec"]


def test_material_and_section_chooses_a_cae_material_from_its_list(tmp_path):
    # the same library material, chosen on the node itself instead of through an Input
    seed = "from abaqus import mdb\nm = mdb.Model(name='Lib')\nm.Material(name='Steel')\n"
    nodes = [("m", "model.model", {"name": "Work"}), ("s", "model.sketch", {}),
             ("p", "model.part", {"body": "Solid", "method": "Extrude"}),
             ("sec", "model.section", {"section": "Own", "cae_material": "Lib|Steel",
                                       "behavior": "Lamina"})]  # hidden settings don't apply
    edges = [("m", "model", "p", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "sec", "target")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    text = out["script"]
    assert "m.Material(name=" not in text
    assert "m.copyMaterials(sourceModel=_source, names=('Steel',))" in text
    assert "HomogeneousSolidSection(name='Own', material='Steel'" in text
    log, stdout = run_in_fake_cae([seed, text], tmp_path)
    assert "failed" not in stdout, stdout
    # nothing chosen: the node's own material, as before
    out, bad = script(nodes[:3] + [("sec", "model.section", {"section": "Own"})], edges)
    assert not bad, bad
    assert "Material(name='Material-1')" in out["script"]
    # a wired Input wins over the list
    out, bad = script(nodes + [("pick", "input.value", {"type": "Material in CAE", "value": "Lib|Alu"})],
                      edges + [("pick", "value", "sec", "existing")])
    assert not bad, bad
    assert "names=('Alu',)" in out["script"]


def test_a_face_region_seeds_its_own_edges(tmp_path):
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("mesh", "model.mesh", {"size": 10}), ("hole", "model.region", {"kind": "Face", "value": "1"}),
             ("fine", "model.mesh", {"size": 1, "seed_on": "Its edges"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "mesh", "target"), ("mesh", "part", "hole", "source"),
             ("hole", "region", "fine", "target")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    text = out["script"]
    assert text.count("part.seedPart(") == 1  # only the first Mesh's global seed
    assert "for k in _d.getEdges()" in text and "seedEdgeBySize(edges=mesh_edges, size=1.0" in text
    log, stdout = run_in_fake_cae(text, tmp_path)
    assert "failed" not in stdout, stdout
    # by default (Whole part) a Face Region still seeds the part as before: saved graphs don't change
    nodes[-1] = ("fine", "model.mesh", {"size": 1})
    assert script(nodes, edges)[0]["script"].count("part.seedPart(") == 2


def test_everything_done_to_a_part_on_any_branch_builds_that_one_part(tmp_path):
    # Part -> Material & Section and Part -> Mesh side by side, a Part Feature on a third branch, the Instance from
    # the bare Part: one CAE part with the section, the feature and the mesh, and the instance is of it
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("sec", "model.section", {}), ("mesh", "model.mesh", {"size": 5}),
             ("f", "model.feature", {"operation": "Remove redundant entities", "name": "Tidy"}),
             ("i", "model.instance", {})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"), ("p", "part", "mesh", "target"),
             ("p", "part", "f", "part"), ("p", "part", "i", "part")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    text = out["script"]
    assert re.findall(r"\('part\|[^|]+\|([^']+)'", text) == ["Part-1"]
    assert text.count("part.SectionAssignment(") == 1 and text.count("part.generateMesh()") == 1
    assert "'feature|NoCode-Model|Part-1|Tidy'" in text
    log, stdout = run_in_fake_cae(text, tmp_path)
    assert "failed" not in stdout, stdout
    assert [c["kwargs"]["part"] for c in calls(log, ".Instance")][-1].endswith("parts[Part-1]>")
    # a list of sizes still makes separately named parts (a convergence study)
    nodes[3] = ("mesh", "model.mesh", {})
    nodes.append(("sizes", "input.value", {"type": "List / matrix", "value": "10, 5"}))
    edges.append(("sizes", "value", "mesh", "param:size"))
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert re.findall(r"\('part\|[^|]+\|([^']+)'", out["script"]) == ["Part-1", "Part-1-2"]  # one per size


def test_an_instance_of_a_mesh_refined_by_a_second_mesh_is_one_part(tmp_path):
    # Mesh -> Instance, and Mesh -> Edge Region -> Mesh (a local seed): one part, meshed with both, instanced once;
    # with or without a Part Feature before (the owner's Defeature graph made a copy, Flange-2)
    for featured in (False, True):
        nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
                 ("mesh", "model.mesh", {"size": 10}), ("rims", "model.region", {"kind": "Edge", "value": "0, 2"}),
                 ("fine", "model.mesh", {"size": 2}), ("i", "model.instance", {})]
        edges = [("s", "sketch", "p", "profile"), ("p", "part", "mesh", "target"), ("mesh", "part", "rims", "source"),
                 ("rims", "region", "fine", "target"), ("mesh", "part", "i", "part")]
        if featured:
            nodes.append(("f", "model.feature", {"operation": "Remove redundant entities"}))
            edges[1:2] = [("p", "part", "f", "part"), ("f", "part", "mesh", "target")]
        out, bad = script(nodes, edges)
        assert not bad, bad
        parts = re.findall(r"\('part\|[^|]+\|([^']+)'", out["script"])
        assert parts == ["Part-1"], (featured, parts)
        assert "seedEdgeBySize" in out["script"] and "instance|NoCode-Model|Part-1-1" in out["script"]
        log, stdout = run_in_fake_cae(out["script"], tmp_path)
        assert "failed" not in stdout, stdout
        assert [c["kwargs"]["part"] for c in calls(log, ".Instance")][-1].endswith("parts[Part-1]>")


def test_mesh_seeds_by_number_and_bias_and_measures_its_mesh(tmp_path):
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("all", "model.mesh", {"seed": "By number", "number": 6}),
             ("edge", "model.region", {"kind": "Edge", "value": "0, 2"}),
             ("graded", "model.mesh", {"seed": "Biased", "number": 12, "ratio": 5, "bias": "Fine at both ends"}),
             ("i", "model.instance", {}), ("face", "model.region", {"kind": "Face", "value": "1"}),
             ("nodes", "model.measure", {"quantity": "Nodes"}),
             ("worst", "model.measure", {"quantity": "Worst aspect ratio"}),
             ("failed", "model.measure", {"quantity": "Failed elements"}),
             ("faced", "model.measure", {"quantity": "Elements"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "all", "target"), ("all", "part", "edge", "source"),
             ("edge", "region", "graded", "target"), ("graded", "part", "i", "part"), ("i", "instance", "face", "source"),
             ("graded", "part", "nodes", "first"), ("graded", "part", "worst", "first"),
             ("i", "instance", "failed", "first"), ("face", "region", "faced", "first")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    code = out["script"]
    assert "part.seedEdgeByNumber(edges=part.edges[:], number=6, constraint=FINER)" in code
    assert "part.seedEdgeByBias(biasMethod=DOUBLE, endEdges=mesh_edges, ratio=5.0, number=12, constraint=FINER)" in code
    assert ".verifyMeshQuality(criterion=ASPECT_RATIO, threshold=10.0)" in code
    assert ".rootAssembly.verifyMeshQuality(criterion=ANALYSIS_CHECKS, regions=(_i.cells[:]" in code
    assert "len(set(_x.label for _e in first for _x in _e.getElements()))" in code
    log, stdout = run_in_fake_cae(code, tmp_path)
    assert "failed" not in stdout.replace("Failed elements", ""), stdout
    answers = json.loads(next(ln for ln in stdout.splitlines() if ln.startswith(cae_queries.MARKER))[
        len(cae_queries.MARKER):])
    assert sorted(answers.values()) == [0.0, 0.0, 0.0, 2.1]  # the fake has no nodes; its worst ratio is 2.1

    for change, message in (({"seed": "Biased"}, "needs an Edge Region"), ({"seed": "By number", "number": 0},
                                                                          "at least 1")):
        _, res, _ = evaluate([("s", "model.sketch", {}), ("p", "model.part", {}), ("m", "model.mesh", change)],
                             [("s", "sketch", "p", "profile"), ("p", "part", "m", "target")])
        assert res["m"]["status"] == "error" and message in res["m"]["message"], res["m"]
    _, res, _ = evaluate([("s", "model.sketch", {}), ("p", "model.part", {}),
                          ("n", "model.measure", {"quantity": "Nodes"})],
                         [("s", "sketch", "p", "profile"), ("p", "part", "n", "first")])
    assert res["n"]["status"] == "error" and "connect Mesh" in res["n"]["message"]


def test_connectors_join_points_couplings_and_vertices(tmp_path):
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("i", "model.instance", {}), ("face", "model.region", {"kind": "Face", "value": "0"}),
             ("corner", "model.region", {"kind": "Vertex", "value": "0"}), ("step", "model.step", {"history": "CTF, CU"}),
             ("rbe", "model.interaction", {"name": "Grip", "kind": "Kinematic coupling", "point": "50, 5, 5"}),
             ("spring", "model.interaction", {"name": "Spring", "kind": "Connector", "connector": "Bushing",
                                              "stiffness": "100, 100, 100, 5, 5, 5", "damping": "2",
                                              "point2": "80, 5, 5"}),
             ("link", "model.interaction", {"name": "Link", "kind": "Connector", "point": "0, 0, 0",
                                            "point2": "0, 0, 30"}),
             ("ground", "model.interaction", {"name": "Grounded", "kind": "Connector", "second": "Ground"}),
             ("pin", "model.interaction", {"name": "Pin", "kind": "Connector", "connector": "Join"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "i", "part"), ("i", "instance", "face", "source"),
             ("i", "instance", "corner", "source"), ("face", "region", "rbe", "slave"),
             ("rbe", "interaction", "spring", "master"), ("step", "step", "link", "step"),
             ("rbe", "interaction", "ground", "master"),
             ("rbe", "interaction", "pin", "master"), ("corner", "region", "pin", "slave"),
             ("spring", "interaction", "step", "history_region")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    code = out["script"]
    assert "end1 = _set.referencePoints[0] if len(_set.referencePoints) else _set.vertices[0]" in code
    assert "a.features.changeKey(fromName=_rp.name, toName='Spring-RP2')" in code
    assert "m.ConnectorSection(name='Spring-Section', assembledType=BUSHING)" in code
    assert ("behaviorOptions=(connectorBehavior.ConnectorElasticity(components=(1, 2, 3, 4, 5, 6), "
            "table=((100.0, 100.0, 100.0, 5.0, 5.0, 5.0),)), connectorBehavior.ConnectorDamping(components=(1, 2, 3, "
            "4, 5, 6), table=((2.0, 2.0, 2.0, 2.0, 2.0, 2.0),)),)") in code
    assert "m.ConnectorSection(name='Link-Section', assembledType=BEAM)" in code
    assert "end2 = None" in code and "Grounded-RP2" not in code
    assert "m.ConnectorSection(name='Grounded-Section', assembledType=BEAM)" in code
    assert "a.ConnectorOrientation(region=a.sets['Spring-Wire'], localCsys1=a.datums[_c.id])" in code
    assert "Link-Csys" not in code
    assert "m.ConnectorSection(name='Pin-Section', translationalType=JOIN)" in code
    assert "a.SectionAssignment(region=a.sets['Pin-Wire'], sectionName='Pin-Section')" in code
    assert "history_region = mdb.models['NoCode-Model'].rootAssembly.sets['Spring-Wire']" in code
    log, stdout = run_in_fake_cae(code, tmp_path)
    assert "failed" not in stdout, stdout
    assert len(calls(log, ".WirePolyLine")) == 4 and len(calls(log, ".SectionAssignment")) == 4

    for change, message in (({"connector": "Beam", "stiffness": "5"}, "is rigid"),
                            ({"connector": "Bushing", "stiffness": "1, 2"}, "one per free component"),
                            ({"connector": "Join", "damping": "-1"}, "damping must be"),
                            ({"point2": "0, 0, 0"}, "two points are the same")):
        _, res, _ = evaluate(nodes[:6] + [("bad", "model.interaction", dict({"kind": "Connector"}, **change))],
                             edges[:5] + [("step", "step", "bad", "step")])
        assert res["bad"]["status"] == "error" and message in res["bad"]["message"], (change, res["bad"])


def test_general_contact_whole_model_self_or_pair(tmp_path):
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("i", "model.instance", {}), ("a", "model.region", {"kind": "Face", "value": "0"}),
             ("b", "model.region", {"kind": "Face", "value": "1"}), ("step", "model.step", {"name": "Squeeze"}),
             ("all", "model.interaction", {"name": "Everything", "kind": "General contact", "friction": 0.3}),
             ("pair", "model.interaction", {"name": "Pair", "kind": "General contact", "friction": 0}),
             ("self", "model.interaction", {"name": "Self", "kind": "General contact"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "i", "part"), ("i", "instance", "a", "source"),
             ("i", "instance", "b", "source"), ("step", "step", "all", "step"), ("step", "step", "pair", "step"),
             ("a", "region", "pair", "master"), ("b", "region", "pair", "slave"), ("step", "step", "self", "step"),
             ("b", "region", "self", "slave")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    code = out["script"]
    assert "m.ContactStd(name='Everything', createStepName='Initial')" in code
    assert "includedPairs.setValuesInStep(stepName='Initial', useAllstar=ON)" in code
    assert "table=((0.3,),)" in code and "TangentialBehavior(formulation=FRICTIONLESS)" in code
    assert "addPairs=((a.surfaces['Pair-A'], a.surfaces['Pair-B']),)" in code
    assert "addPairs=((a.surfaces['Self-A'], SELF),)" in code
    assert "assignments=((GLOBAL, SELF, 'Everything-Property'),)" in code
    log, stdout = run_in_fake_cae(code, tmp_path)
    assert "failed" not in stdout, stdout
    assert len(calls(log, ".ContactStd")) == 3

    _, res, _ = evaluate(nodes[:5] + [("bad", "model.interaction", {"kind": "General contact"})], edges[:4])
    assert res["bad"]["status"] == "error" and "needs a Step" in res["bad"]["message"]


@pytest.mark.parametrize("sketch, command", [
    ({"shape": "Polygon", "sides": 6, "radius": 10, "rotation": 15}, ".Line"),
    ({"shape": "Ellipse", "width": 40, "height": 20, "rotation": 30}, ".EllipseByCenterPerimeter"),
    ({"shape": "Slot", "width": 20, "height": 60}, ".ArcByCenterEnds"),
    ({"shape": "Rectangle", "corner": 10}, ".ArcByCenterEnds"),
])
def test_sketch_shapes_build_closed_profiles(tmp_path, sketch, command):
    nodes = [("s", "model.sketch", dict({"x": 50, "y": 25}, **sketch)),
             ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 1})]
    out, bad = script(nodes, [("s", "sketch", "p", "profile")])
    assert not bad, bad
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, command) and calls(log, ".BaseSolidExtrude")
    _, res, inputs = evaluate(nodes, [("s", "sketch", "p", "profile")])
    outline = inputs.outputs["s"]["sketch"].outline()[0]
    assert outline[0] == pytest.approx(outline[-1])  # closed, for the preview too


def test_polygon_slot_and_rounded_corners_have_the_right_size():
    from nodecode_fea.values import Sketch

    def area(item):
        pts = Sketch("s", (item,)).outline()[0]
        return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:]))) / 2

    assert area(("Polygon", (0, 0), 10.0, 6, 0.0)) == pytest.approx(3 * 3 ** 0.5 / 2 * 100, rel=1e-6)
    assert area(("Slot", (0, 0), 60.0, 20.0, 0.0)) == pytest.approx(800 + math.pi * 100, rel=2e-3)
    assert area(("Rectangle", (0, 0), (100, 50), 10.0)) == pytest.approx(5000 - (4 - math.pi) * 100, rel=2e-3)
    for bad, message in (({"shape": "Polygon", "sides": 2}, "3 to 360"), ({"shape": "Rectangle", "corner": 30},
                                                                        "half the shorter side"),
                         ({"shape": "Slot", "width": 20, "height": 20}, "use Circle")):
        _, res, _ = evaluate([("s", "model.sketch", bad)], [])
        assert res["s"]["status"] == "error" and message in res["s"]["message"], (bad, res["s"])


def test_for_by_count_linear_and_geometric():
    def seq(**params):
        _, res, inputs = evaluate([("f", "loop.for", params)], [])
        assert res["f"]["status"] == "ok", res["f"]
        return [n.value for n in inputs.outputs["f"]["i"]]

    assert seq(start=1, stop=10, step=3) == [1, 4, 7, 10]
    assert seq(by="Count", start=0, stop=1, count=5) == [0, 0.25, 0.5, 0.75, 1]
    assert seq(by="Count", start=1, stop=1000, count=4, spacing="Geometric") == pytest.approx([1, 10, 100, 1000])
    assert seq(by="Count", start=5, stop=9, count=1) == [5]
    _, res, _ = evaluate([("f", "loop.for", {"by": "Count", "start": -1, "stop": 10, "spacing": "Geometric"})], [])
    assert res["f"]["status"] == "error" and "same sign" in res["f"]["message"]


def test_region_by_shape_finds_cylinders_circular_faces_edges_and_flat_faces():
    base = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10})]
    wire = [("s", "sketch", "p", "profile"), ("p", "part", "r", "source")]
    cases = (({"shape": "Cylindrical faces", "radius": 10, "tolerance": 0.01}, "face",
              "(('@shape', 'cylinder', 10.0, 0.01),)", "all cylindrical faces of radius 10"),
             ({"shape": "Circular faces", "radius": 5}, "face",
              "(('@shape', 'disk', 5.0, 0.0),)", "all circular faces of radius 5"),
             ({"shape": "Circular edges"}, "edge", "(('@shape', 'circle', 0.0, 0.0),)", "all circular edges"),
             ({"shape": "Flat faces", "facing": "0, 0, 1"}, "face", "(('@shape', 'plane', 0.0, 0.0, 1.0),)",
              "all flat faces facing 0, 0, 1"))
    for settings, kind, spec, summary in cases:
        nodes = base + [("r", "model.region", dict({"method": "By shape", "create": "Set", "name": "Holes"},
                                                     **settings))]
        out, bad = script(nodes, wire)
        assert not bad, bad
        assert spec in out["script"]
        _, res, inputs = evaluate(nodes, wire)
        region = inputs.outputs["r"]["region"]
        assert region.kind == kind and region.summary() == summary + " as Set Holes"
    _, res, _ = evaluate(base + [("r", "model.region", {"method": "By shape", "shape": "Flat faces", "facing": "0, 0, 0"})],
                         wire)
    assert res["r"]["status"] == "error" and "direction" in res["r"]["message"]


def test_one_region_circular_face_selection_maps_couplings_and_ground_connectors(monkeypatch):
    """One chain maps over every segment and its three CAE-located support faces."""
    local_centers = [[10.0, 20.0, 0.0], [-5.0, 28.0, 0.0], [-5.0, 12.0, 0.0]]

    def centers(quantity, first, second=None):
        shift = sum(step[1][0] for step in first.source.steps if step[0] == "move")
        return [[x + shift, y, z] for x, y, z in local_centers]

    monkeypatch.setattr(cae_queries, "value", centers)
    nodes = [
        ("s", "model.sketch", {}),
        ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
        ("x", "input.value", {"type": "List / matrix", "value": "[0, 100]"}),
        ("i", "model.instance", {}),
        ("pads", "model.region", {"method": "By shape", "shape": "Circular faces", "radius": 5,
                                    "members": "One per geometry"}),
        ("supports", "model.interaction", {"name": "Support", "kind": "Distributing coupling",
                                             "point_from": "Slave Region point", "dofs": "U1, U2, U3"}),
        ("ground", "model.interaction", {"name": "Ground", "kind": "Connector", "connector": "Beam",
                                           "second": "Ground"}),
    ]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "i", "part"), ("x", "value", "i", "param:x"),
             ("i", "instance", "pads", "source"), ("pads", "region", "supports", "slave"),
             ("supports", "interaction", "ground", "master")]

    graph_data, results, inputs = evaluate(nodes, edges)

    assert all(result["status"] == "ok" for result in results.values()), results
    regions = inputs.outputs["pads"]["region"]
    supports = inputs.outputs["supports"]["interaction"]
    connectors = inputs.outputs["ground"]["interaction"]
    expected = [[tuple(point) for point in local_centers],
                [(x + 100, y, z) for x, y, z in local_centers]]
    assert [[region.points[0] for region in segment] for segment in regions] == expected
    assert [[support.point for support in segment] for segment in supports] == expected
    assert len(connectors) == 2 and all(len(segment) == 3 for segment in connectors)
    assert all(connector.ground for segment in connectors for connector in segment)
    code = build_script(graph_data, results, inputs)["script"]
    assert code.count("m.Coupling(name=") == 6
    assert code.count("a.WirePolyLine(points=((end1, end2),)") == 6
    assert "a.ReferencePoint(point=(10.0, 20.0, 0.0))" in code
    assert "a.ReferencePoint(point=(110.0, 20.0, 0.0))" in code


def test_one_per_geometry_holes_are_found_nearest_their_centre(monkeypatch):
    """A hole's centre is on its axis, in empty space where findAt finds nothing: each hole's Region keeps the By shape
    search plus "@at" its centre (CAE takes the match nearest it), and a coupling's Slave Region point is that centre."""
    monkeypatch.setattr(cae_queries, "value", lambda q, first, second=None: [[30.0, 25.0, 10.0], [70.0, 25.0, 10.0]])
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude"}),
             ("i", "model.instance", {}),
             ("holes", "model.region", {"method": "By shape", "radius": 5, "members": "One per geometry"}),
             ("bolts", "model.interaction", {"name": "Bolt", "kind": "Kinematic coupling",
                                             "point_from": "Slave Region point"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "i", "part"), ("i", "instance", "holes", "source"),
             ("holes", "region", "bolts", "slave")]

    graph_data, results, inputs = evaluate(nodes, edges)

    assert all(result["status"] == "ok" for result in results.values()), results
    holes = inputs.outputs["holes"]["region"]
    assert [h.at for h in holes] == [(30.0, 25.0, 10.0), (70.0, 25.0, 10.0)]
    assert holes[0].points == (("@shape", "cylinder", 5.0, 0.0, "@at", 30.0, 25.0, 10.0),)
    assert holes[0].summary() == "the cylindrical face of radius 5 at 30, 25, 10"
    assert [b.point for b in inputs.outputs["bolts"]["interaction"]] == [(30.0, 25.0, 10.0), (70.0, 25.0, 10.0)]
    code = build_script(graph_data, results, inputs)["script"]
    assert "('@shape', 'cylinder', 5.0, 0.0, '@at', 30.0, 25.0, 10.0)" in code
    assert code.count("m.Coupling(name=") == 2


def test_one_per_geometry_first_sync_asks_cae_for_centres(monkeypatch):
    def pending(quantity, first, second=None):
        return PendingMeasure("pad-centres", quantity, first, second)

    monkeypatch.setattr(cae_queries, "value", pending)
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude"}),
             ("i", "model.instance", {}),
             ("pads", "model.region", {"method": "By shape", "shape": "Circular faces", "radius": 5,
                                         "members": "One per geometry"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "i", "part"),
             ("i", "instance", "pads", "source")]

    graph_data, results, inputs = evaluate(nodes, edges)
    code = build_script(graph_data, results, inputs)["script"]

    assert results["pads"]["status"] == "ok"
    assert "('@shape', 'disk', 5.0, 0.0)" in code
    assert "answers['pad-centres']" in code and cae_queries.MARKER in code


def test_partition_then_section_and_mesh_is_one_part_meshed_after_the_partition():
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude"}),
             ("d", "model.datum", {"kind": "Principal plane", "plane": "YZ", "offset": 50}),
             ("f", "model.feature", {"operation": "Partition"}), ("sec", "model.section", {}),
             ("mesh", "model.mesh", {}), ("i", "model.instance", {})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "d", "part"), ("p", "part", "f", "part"),
             ("d", "datum", "f", "datum"), ("f", "part", "sec", "target"), ("sec", "part", "mesh", "target"),
             ("mesh", "part", "i", "part")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    code = out["script"]
    assert code.count("m.Part(name=") == 1  # no second copy of the part
    assert code.index("PartitionCellByDatumPlane") < code.index("SectionAssignment") < code.index("generateMesh")
    # a Datum made on the bare Part also works on its sectioned copy
    edges = [e for e in edges if e != ("f", "part", "sec", "target") and e != ("p", "part", "f", "part")]
    edges += [("p", "part", "sec", "target"), ("sec", "part", "f", "part"), ("f", "part", "mesh", "target")]
    edges = [e for e in edges if e != ("sec", "part", "mesh", "target")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert out["script"].count("m.Part(name=") == 1


def test_cae_objects_are_listed_for_an_input_to_choose(monkeypatch):
    from nodecode_fea import app
    sent = {}
    monkeypatch.setattr(app.link, "send", lambda request, **kw: sent.update(kw) or "r1")
    monkeypatch.setattr(app.link, "wait", lambda rid, timeout: {
        "ok": True, "output": app.OBJECTS + '["Hand Model|Bracket-1|Bracket"]\nNodeCode FEA: done.\n'})
    assert app.cae_objects("instance") == {"items": ["Hand Model|Bracket-1|Bracket"]}
    assert "rootAssembly.instances" in sent["script"] and sent["module"] == ""  # CAE stays in its module
    # what CAE lists is what the Input reads
    _, res, inputs = evaluate([("in", "input.value", {"type": "Instance in CAE",
                                                      "value": "Hand Model|Bracket-1|Bracket"})], [])
    assert inputs.outputs["in"]["value"].part.name == "Bracket"


def test_choosing_from_cae_what_the_graph_built_is_that_object(tmp_path):
    """The graph builds sketch Bore in BoltCircle; an Input chooses BoltCircle|Bore from CAE for a second Part. Both
    are the one CAE sketch: no Bore-2, nothing deleted, the Part copies Bore."""
    nodes = [("m", "model.model", {"name": "BoltCircle"}), ("bore", "model.sketch", {"name": "Bore", "shape": "Circle"}),
             ("p1", "model.part", {"name": "Disc"}),
             ("in", "input.value", {"type": "Sketch in CAE", "value": "BoltCircle|Bore"}),
             ("p2", "model.part", {"name": "Copy"})]
    edges = [("m", "model", "bore", "model"), ("bore", "sketch", "p1", "profile"), ("in", "value", "p2", "profile"),
             ("m", "model", "p2", "model")]
    for focus in ("p2", "in"):
        out, bad = script(nodes, edges, focus=focus)
        assert not bad, bad
        assert "Bore-2" not in out["script"] and "built as" not in out["script"]
        log, stdout = run_in_fake_cae(out["script"], tmp_path)
        assert "failed" not in stdout, stdout
        assert not calls(log, "sketches[Bore].__delitem__") and not any(
            "Bore" in str(c.get("args")) for c in calls(log, "__delitem__"))


# ------------------------------------------------------------------ aligning instances (position constraints)

def aligned(how="Face to face", flip="No", moving_on="p", kind="Face"):
    """Two boxes: B's entity 0 (picked on the part) brought onto entity 1 of instance A."""
    nodes = [("m", "model.model", {"name": "M"}), ("s", "model.sketch", {}),
             ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("a", "model.instance", {"name": "A"}),
             ("b", "model.instance", {"name": "B", "x": 300, "how": how, "flip": flip}),
             ("mv", "model.region", {"kind": kind, "value": "0"}), ("fx", "model.region", {"kind": kind, "value": "1"})]
    edges = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "a", "part"),
             ("p", "part", "b", "part"),
             (moving_on, "part" if moving_on == "p" else "instance", "mv", "source"), ("a", "instance", "fx", "source"),
             ("mv", "region", "b", "align"), ("fx", "region", "b", "to")]
    return nodes, edges


def test_an_instance_is_aligned_onto_another_with_a_position_constraint(tmp_path):
    out, bad = script(*aligned())
    assert not bad, bad
    log, _ = run_in_fake_cae(out["script"], tmp_path)
    (f2f,) = [c for c in log if c["call"].endswith("rootAssembly.FaceToFace")]
    assert f2f["kwargs"]["flip"] == "ON" and f2f["kwargs"]["clearance"] == 0.0  # faces against each other
    # after B's own move, and named after it
    order = [c["call"].rsplit(".", 1)[-1] for c in log if c["call"].endswith(("rootAssembly.translate", "FaceToFace"))]
    assert order == ["translate", "FaceToFace"]
    assert "'B-Align-1'" in out["script"]
    b = next(i for i in [inputs_b(aligned())] if i)
    assert b.summary() == "instance of Part-1, moved 300, 0, 0, face to face to A"


def inputs_b(nodes_edges):
    _, _, inputs = evaluate(*nodes_edges)
    return inputs.outputs["b"]["instance"]


@pytest.mark.parametrize("how,method,kind", [("Edge to edge", "EdgeToEdge", "Edge"), ("Coaxial", "Coaxial", "Face"),
                                             ("Point to point", "CoincidentPoint", "Vertex")])
def test_every_alignment_is_its_cae_constraint(how, method, kind, tmp_path):
    out, bad = script(*aligned(how=how, flip="Yes", kind=kind))
    assert not bad, bad
    log, _ = run_in_fake_cae(out["script"], tmp_path)
    (c,) = [c for c in log if c["call"].endswith("rootAssembly." + method)]
    assert c["kwargs"].get("flip", "ON") == "ON"


@pytest.mark.parametrize("change,message", [
    (lambda n, e: (n, e[:-1]), "needs both"),
    (lambda n, e: (n, [x if x[2:] != ("fx", "source") else ("p", "part", "fx", "source") for x in e]), "another instance"),
    (lambda n, e: (n, [x if x[2:] != ("fx", "source") else ("b", "instance", "fx", "source") for x in e]), None),
    (lambda n, e: ([x if x[0] != "mv" else ("mv", "model.region", {"kind": "Edge", "value": "0"}) for x in n], e),
     "needs a face Region in Align"),
])
def test_alignment_explains_what_it_needs(change, message):
    _, res, _ = evaluate(*change(*aligned()))
    if message is None:  # To on B itself is a cycle: B waits on its own region
        assert res["b"]["status"] != "ok"
        return
    assert res["b"]["status"] == "error" and message in res["b"]["message"], res["b"]


def test_a_moved_fixed_instance_rebuilds_the_aligned_one():
    from nodecode_fea.cae_model import collect
    g, res, inputs = evaluate(*aligned())
    objs, _, _ = collect(g, res, inputs)
    b = next(o for o in objs if o["key"].endswith("|B"))
    a = next(o for o in objs if o["key"].endswith("|A"))
    assert a["key"] in b["deps"]


def test_material_hyperelastic_rubber_and_rayleigh_damping(tmp_path):
    base = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "sec", "target")]
    out, bad = script(base + [("sec", "model.section", {"behavior": "Mooney-Rivlin", "c10": 0.3, "c01": 0.05,
                                                        "d1": 0.01, "alpha": 2, "beta": 1e-4})], edges)
    assert not bad, bad
    assert ("material.Hyperelastic(materialType=ISOTROPIC, testData=OFF, type=MOONEY_RIVLIN, "
            "volumetricResponse=VOLUMETRIC_DATA, table=((0.3, 0.05, 0.01),))") in out["script"]
    assert "material.Elastic" not in out["script"]
    assert "material.Damping(alpha=2.0, beta=0.0001)" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    out, bad = script(base + [("sec", "model.section", {"behavior": "Neo-Hooke", "c10": 0.5})], edges)
    assert not bad and "type=NEO_HOOKE" in out["script"] and "table=((0.5, 0.0),)" in out["script"]
    _, bad = script(base + [("sec", "model.section", {"behavior": "Neo-Hooke", "c10": 0})], edges)
    assert "C10 + C01 > 0" in bad["sec"]


def test_what_cae_cannot_build_goes_on_the_node_that_makes_it(tmp_path):
    from nodecode_fea import checks
    nodes, edges = aligned()
    # the fake CAE finds nothing at a point: B's alignment fails in CAE, though the graph is fine
    nodes = [("mv", "model.region", {"method": "Find at", "kind": "Face"}) if n[0] == "mv" else n for n in nodes]
    nodes.append(("at", "input.value", {"type": "Text", "value": "1, 2, 3"}))
    out, bad = script(nodes, edges + [("at", "value", "mv", "points")])
    assert not bad, bad
    _, stdout = run_in_fake_cae(out["script"], tmp_path)
    found = checks.cae_failures(stdout, out["node_keys"])
    assert list(found) == ["b"] and found["b"][0].startswith("In CAE: Align region found no geometry"), found


L_SHAPE = "0, 0; 100, 0; 100, 20; 20, 20; 20, 80; 0, 80"


def test_profile_and_polygon_corners_are_rounded_by_tangent_arcs(tmp_path):
    from nodecode_fea.values import sketch_segments
    g, res, inputs = evaluate([("s", "model.sketch", {"shape": "Profile", "points": L_SHAPE, "closed": "Closed",
                                                      "corner": 5})], [])
    assert res["s"]["status"] == "ok", res["s"]
    (item,) = inputs.outputs["s"]["sketch"].items
    segs = sketch_segments(item)
    assert [s[0] for s in segs].count("arc") == 6 and [s[0] for s in segs].count("line") == 6
    for s in segs:
        if s[0] == "arc":  # both ends on the circle
            assert all(math.isclose(math.dist(s[1], p), 5.0, abs_tol=1e-6) for p in s[2:])
    # the inside corner (20, 20) is rounded clockwise: its arc's centre is outside the L
    inner = next(s for s in segs if s[0] == "arc" and s[1][0] > 20 and s[1][1] > 20)
    assert inner[1] == (25.0, 25.0)
    # the preview outline is one continuous loop
    (line,) = inputs.outputs["s"]["sketch"].outline()
    assert max(math.dist(a, b) for a, b in zip(line, line[1:])) <= 90 + 1e-9  # the longest side, between fillets
    area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(line, line[1:]))) / 2
    assert math.isclose(area, 3200 - 4 * (1 - math.pi / 4) * 25, rel_tol=1e-3)
    out, bad = script([("s", "model.sketch", {"shape": "Polygon", "sides": 6, "corner": 3})], [])
    assert not bad and out["script"].count("ArcByCenterEnds") == 6
    _, res, _ = evaluate([("s", "model.sketch", {"shape": "Profile", "points": L_SHAPE, "closed": "Closed",
                                                 "corner": 15})], [])
    assert "too big for the corner" in res["s"]["message"]


def test_mesh_element_must_fit_the_element_shape():
    base = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "mesh", "target")]
    out, bad = script(base + [("mesh", "model.mesh", {"shape": "Hex-dominated", "element": "C3D20R"})], edges)
    assert not bad, bad
    assert "elemShape=HEX_DOMINATED" in out["script"] and "elemCode=C3D20R" in out["script"]
    _, bad = script(base + [("mesh", "model.mesh", {"shape": "Hex", "element": "C3D10"})], edges)
    assert "C3D10 is a tet element" in bad["mesh"], bad


def bolted(then="Fix its length", region_on="i", later_step=True):
    """A pin cut across at mid-length (Datum + Partition), meshed and placed, held at both ends and preloaded across
    the cut face in a first step, then a second step."""
    nodes = [("m", "model.model", {"name": "Bolt"}), ("s", "model.sketch", {"shape": "Circle", "radius": 5}),
             ("p", "model.part", {"name": "Pin", "body": "Solid", "method": "Extrude", "depth": 40}),
             ("d", "model.datum", {"kind": "Principal plane", "plane": "XY", "offset": 20}),
             ("f", "model.feature", {"operation": "Partition"}), ("sec", "model.section", {}),
             ("mesh", "model.mesh", {"size": 3}), ("i", "model.instance", {}),
             ("at", "input.value", {"type": "Text", "value": "1, 1, 20"}),
             ("cut", "model.region", {"method": "Find at", "kind": "Face"}),
             ("ends", "input.value", {"type": "Text", "value": "1, 1, 0; 1, 1, 40"}),
             ("end", "model.region", {"method": "Find at", "kind": "Face"}),
             ("pre", "model.step", {"name": "Preload"}),
             ("fix", "model.condition", {"name": "Ends", "kind": "Fixed"}),
             ("bolt", "model.condition", {"name": "Pretension", "kind": "Bolt load", "force": 5000, "then": then})]
    edges = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "d", "part"),
             ("p", "part", "f", "part"), ("d", "datum", "f", "datum"), ("f", "part", "sec", "target"),
             ("sec", "part", "mesh", "target"), ("mesh", "part", "i", "part"),
             (region_on, "instance" if region_on == "i" else "part", "cut", "source"), ("at", "value", "cut", "points"),
             ("i", "instance", "end", "source"), ("ends", "value", "end", "points"),
             ("m", "model", "pre", "after"), ("pre", "step", "fix", "step"), ("end", "region", "fix", "region"),
             ("fix", "condition", "bolt", "step"), ("cut", "region", "bolt", "region")]
    if later_step:
        nodes.append(("work", "model.step", {"name": "Work"}))
        edges.append(("pre", "step", "work", "after"))
    return nodes, edges


def test_bolt_load_preloads_across_a_face_then_fixes_its_length(tmp_path):
    from nodecode_fea.cae_model import collect
    out, bad = script(*bolted())
    assert not bad, bad
    assert "m.BoltLoad(name='Pretension', createStepName='Preload', region=target_region, magnitude=5000.0, " \
           "boltMethod=APPLY_FORCE, datumAxis=a.datums[_axis.id])" in out["script"]
    assert "setValuesInStep(stepName=_later, boltMethod=FIX_LENGTH)" in out["script"]
    # built after every step of the model, so the later steps exist when it's fixed in them
    g, res, inputs = evaluate(*bolted())
    objs, _, _ = collect(g, res, inputs)
    keys = [o["key"] for o in objs]
    assert keys.index("condition|Bolt|load|Pretension") > keys.index("step|Bolt|Work")
    out, bad = script(*bolted(then="Keep the force"))
    assert not bad and "FIX_LENGTH" not in out["script"]


def test_bolt_load_needs_one_face_on_an_instance():
    _, res, _ = evaluate(*bolted(region_on="mesh"))
    assert "on an instance" in res["bolt"]["message"], res["bolt"]


def bolted_joint(directory="."):
    """Two drilled plates clamped by a bolt (head and nut in one revolved part): the bolt is made coaxial with the
    top plate's hole and its head put on the top plate, cut at mid-shank and preloaded; head and nut tied to the
    plates, general contact between everything, the bottom plate clamped and the top one pushed sideways."""
    def at(id_, src, kind, point, part=False):
        return ([(id_ + "-at", "input.value", {"type": "Text", "value": point}),
                 (id_, "model.region", {"method": "Find at", "kind": kind})],
                [(src, "part" if part else "instance", id_, "source"), (id_ + "-at", "value", id_, "points")])
    nodes = [("m", "model.model", {"name": "BoltedJoint"}),
             ("hole", "model.sketch", {"name": "Hole", "shape": "Circle", "radius": 6}),
             ("ps", "model.sketch", {"name": "Plate", "x": -30, "y": -20, "width": 60, "height": 40}),
             ("plate", "model.part", {"name": "Plate", "body": "Solid", "method": "Extrude", "depth": 10}),
             ("psec", "model.section", {"material": "Steel", "section": "Plate"}),
             ("pmesh", "model.mesh", {"size": 4}),
             ("bottom", "model.instance", {"name": "Bottom"}), ("top", "model.instance", {"name": "Top", "z": 10}),
             ("bs", "model.sketch", {"name": "Bolt", "shape": "Profile", "closed": "Closed",
                                     "points": "0, 0; 10, 0; 10, 5; 6, 5; 6, 25; 10, 25; 10, 30; 0, 30"}),
             ("bolt", "model.part", {"name": "Bolt", "body": "Solid", "method": "Revolve"}),
             ("cut", "model.datum", {"name": "Mid-Shank", "kind": "Principal plane", "plane": "XZ", "offset": 15}),
             ("split", "model.feature", {"operation": "Partition"}),
             ("bsec", "model.section", {"material": "Bolt-Steel", "section": "Bolt"}),
             ("bmesh", "model.mesh", {"size": 2, "shape": "Tet", "element": "C3D10"}),
             ("shank", "model.region", {"method": "By shape", "shape": "Cylindrical faces", "radius": 6,
                                        "tolerance": 0.01}),
             ("bore", "model.region", {"method": "By shape", "shape": "Cylindrical faces", "radius": 6,
                                       "tolerance": 0.01}),
             ("b1", "model.instance", {"name": "Bolt-1", "x": 100, "how": "Coaxial"}),
             ("b2", "model.instance", {"how": "Face to face"}),
             ("pre", "model.step", {"name": "Preload"}), ("pull", "model.step", {"name": "Pull"}),
             ("headtie", "model.interaction", {"name": "Head", "kind": "Tie"}),
             ("nuttie", "model.interaction", {"name": "Nut", "kind": "Tie"}),
             ("touch", "model.interaction", {"name": "Touch", "kind": "General contact", "friction": 0.2}),
             ("fix", "model.condition", {"name": "Clamp", "kind": "Fixed"}),
             ("preload", "model.condition", {"name": "Pretension", "kind": "Bolt load", "force": 10000}),
             ("push", "model.condition", {"name": "Push", "kind": "Pressure", "magnitude": 20}),
             ("j", "model.job", {"name": "BoltedJoint", "directory": directory, "action": "Write input only"})]
    edges = [("m", "model", "hole", "model"), ("m", "model", "ps", "model"), ("hole", "sketch", "ps", "add"),
             ("ps", "sketch", "plate", "profile"), ("plate", "part", "psec", "target"), ("psec", "part", "pmesh", "target"),
             ("pmesh", "part", "bottom", "part"), ("pmesh", "part", "top", "part"),
             ("m", "model", "bs", "model"), ("bs", "sketch", "bolt", "profile"), ("bolt", "part", "cut", "part"),
             ("bolt", "part", "split", "part"), ("cut", "datum", "split", "datum"), ("split", "part", "bsec", "target"),
             ("bsec", "part", "bmesh", "target"), ("bmesh", "part", "shank", "source"), ("top", "instance", "bore", "source"),
             ("bmesh", "part", "b1", "part"), ("shank", "region", "b1", "align"), ("bore", "region", "b1", "to"),
             ("b1", "instance", "b2", "part"),
             ("m", "model", "pre", "after"), ("pre", "step", "pull", "after"),
             ("pre", "step", "touch", "step"), ("pre", "step", "fix", "step"), ("fix", "condition", "preload", "step"),
             ("pull", "step", "push", "step"), ("push", "condition", "j", "analysis")]
    for n, e in (at("underhead", "bmesh", "Face", "8, 5, 0", part=True), at("topface", "top", "Face", "25, 15, 20"),
                 at("nutface", "bmesh", "Face", "8, 25, 0", part=True), at("bottomface", "bottom", "Face", "25, 15, 0"),
                 at("end", "bottom", "Face", "-30, 0, 5"), at("cutface", "b2", "Face", "1, 1, 10"),
                 at("side", "top", "Face", "30, 0, 15")):
        nodes += n
        edges += e
    edges += [("underhead", "region", "b2", "align"), ("topface", "region", "b2", "to"),
              ("topface", "region", "headtie", "master"), ("underhead", "region", "headtie", "slave"),
              ("bottomface", "region", "nuttie", "master"), ("nutface", "region", "nuttie", "slave"),
              ("end", "region", "fix", "region"), ("cutface", "region", "preload", "region"),
              ("side", "region", "push", "region")]
    return nodes, edges


def test_the_bolted_joint_example_builds(tmp_path):
    from nodecode_fea import checks
    nodes, edges = bolted_joint(str(tmp_path))
    g, res, inputs = evaluate(nodes, edges)
    bad = {k: v["message"] for k, v in res.items() if v["status"] != "ok"}
    assert not bad, bad
    assert not checks.lint(g, res, inputs), checks.lint(g, res, inputs)
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "m.BoltLoad(name='Pretension'" in out["script"] and "a.Coaxial(" in out["script"]


def test_defeaturing_removes_small_holes_found_by_shape(tmp_path):
    """Region By shape (cylindrical faces, radius up to 3) -> Part Feature Remove faces: CAE removes and stitches
    them, names the feature after the node and says how the volume changed (a note on the node)."""
    from nodecode_fea import checks
    nodes = [("base", "model.sketch", {}),
             ("part", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("holes", "model.region", {"method": "By shape", "shape": "Cylindrical faces", "radius": 3,
                                        "match": "Up to"}),
             ("f", "model.feature", {"operation": "Remove faces", "name": "No small holes"})]
    edges = [("base", "sketch", "part", "profile"), ("part", "part", "holes", "source"),
             ("part", "part", "f", "part"), ("holes", "region", "f", "region")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    text = out["script"]
    assert "'@shape', 'cylinder', 3.0, 0.0, 'Up to'" in text and "part.RemoveFacesAndStitch(faceList=targets)" in text
    log, stdout = run_in_fake_cae(text, tmp_path)
    # the fake finds no faces by shape (its parts have no geometry): what CAE says then
    assert "Region matched 0" in stdout or "found no" in stdout or "failed" in stdout
    picked = [("base", "model.sketch", {}),
              ("part", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
              ("faces", "model.region", {"kind": "Face", "value": "3, 4"}),
              ("f", "model.feature", {"operation": "Remove faces", "name": "No small holes"})]
    edges = [("base", "sketch", "part", "profile"), ("part", "part", "faces", "source"),
             ("part", "part", "f", "part"), ("faces", "region", "f", "region")]
    out, bad = script(picked, edges)
    assert not bad, bad
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, "RemoveFacesAndStitch")
    assert any(c["kwargs"].get("toName") == "No-small-holes" for c in calls(log, "changeKey"))
    notes = checks.cae_failures(stdout, out["node_keys"], checks.NOTE)
    assert notes == {"f": ["In CAE: Remove faces (2 faces) changed the volume by +1 % (1000 to 1010)"]}, stdout
    from nodecode_fea import app as server  # the note stays on the node until it is rebuilt or gone
    doc = server._Doc()
    server._note_cae_failures(doc, stdout, out["node_keys"])
    server._note_cae_failures(doc, "NodeCode FEA: model: 0 built, 4 unchanged, 0 deleted", out["node_keys"])
    assert doc.notes == notes
    server._note_cae_failures(doc, "", {})
    assert not doc.notes
    # followed by more part nodes the feature node is only a stage of the part: its feature is still its own
    out, bad = script(picked + [("mesh", "model.mesh", {"size": 5})], edges + [("f", "part", "mesh", "target")])
    assert not bad, bad
    assert out["node_keys"]["f"] == ["feature|NoCode-Model|Part-1|No-small-holes"]


@pytest.mark.parametrize("operation, kind, call", [("Repair small edges", "Edge", "RepairSmallEdges"),
                                                   ("Repair small faces", "Face", "RepairSmallFaces")])
def test_repairs_take_the_small_ones_of_a_region_or_the_part(tmp_path, operation, kind, call):
    nodes = [("base", "model.sketch", {}), ("part", "model.part", {"body": "Solid", "method": "Extrude"}),
             ("r", "model.region", {"kind": kind, "value": "0, 1"}),
             ("f", "model.feature", {"operation": operation, "size": 0.5})]
    edges = [("base", "sketch", "part", "profile"), ("part", "part", "r", "source"), ("part", "part", "f", "part"),
             ("r", "region", "f", "region")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "getSize(printResults=False) < 0.5" in out["script"] and f"part.{call}(" in out["script"]
    _, bad = script(nodes[:2] + [("f", "model.feature", {"operation": operation})],
                    [edges[0], edges[2]])
    assert "needs a size" in bad["f"]
    out, bad = script(nodes[:2] + [("f", "model.feature", {"operation": "Remove redundant entities"})],
                      [edges[0], edges[2]])
    assert not bad, bad
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout and calls(log, "RemoveRedundantEntities"), stdout
    _, bad = script(nodes[:2] + [("f", "model.feature", {"operation": "Remove faces"})], [edges[0], edges[2]])
    assert "needs a Face Region" in bad["f"]


def test_remove_wire_deletes_an_edge_regions_edges(tmp_path):
    nodes = [("base", "model.sketch", {}), ("part", "model.part", {"body": "Wire", "method": "Planar"}),
             ("r", "model.region", {"kind": "Edge", "value": "0, 1"}),
             ("f", "model.feature", {"operation": "Remove wire", "name": "No-Lines"})]
    edges = [("base", "sketch", "part", "profile"), ("part", "part", "r", "source"), ("part", "part", "f", "part"),
             ("r", "region", "f", "region")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "part.RemoveWireEdges(wireEdgeList=targets)" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout and calls(log, "RemoveWireEdges"), stdout
    _, bad = script(nodes[:2] + [nodes[3]], [edges[0], edges[2]])
    assert "needs an Edge Region" in bad["f"]


@pytest.mark.parametrize("operation, extra_nodes, extra_edges, call", [
    ("Partition by extending a face", [("t", "model.region", {"kind": "Face", "value": "2"})],
     [("part", "part", "t", "source"), ("t", "region", "f", "tool")], "PartitionCellByExtendFace"),
    ("Partition by a sketch on a face", [("t", "model.region", {"kind": "Face", "value": "0"}),
                                         ("k", "model.sketch", {"shape": "Circle", "radius": 3})],
     [("part", "part", "t", "source"), ("t", "region", "f", "region"), ("k", "sketch", "f", "sketch")],
     "PartitionFaceBySketch"),
    ("Split edges", [("t", "model.region", {"kind": "Edge", "value": "0, 3"})],
     [("part", "part", "t", "source"), ("t", "region", "f", "region")], "PartitionEdgeByParam"),
])
def test_partitions_by_face_sketch_and_edge_split(tmp_path, operation, extra_nodes, extra_edges, call):
    nodes = [("base", "model.sketch", {}), ("part", "model.part", {"body": "Solid", "method": "Extrude"}),
             *extra_nodes, ("f", "model.feature", {"operation": operation, "fraction": 0.25})]
    edges = [("base", "sketch", "part", "profile"), ("part", "part", "f", "part"), *extra_edges]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert f"part.{call}(" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    made = calls(log, call)
    assert made and any(c["kwargs"].get("toName") == "Split-1" if call == "PartitionEdgeByParam"
                        else c["kwargs"].get("toName") == "Partition-1" for c in calls(log, "changeKey"))
    if call == "PartitionEdgeByParam":
        assert made[0]["kwargs"]["parameter"] == 0.25
    # what each one needs, said plainly
    _, bad = script(nodes[:2] + [nodes[-1]], edges[:2])
    assert "needs" in bad["f"]


def test_thin_solids_become_shells(tmp_path):
    """Solid to shell / Midsurface: CAE makes the shell; downstream the part is a shell (Shell section on its faces,
    shell elements), and the operation's several features go with its node."""
    plate = [("base", "model.sketch", {}), ("part", "model.part", {"body": "Solid", "method": "Extrude", "depth": 2}),
             ("top", "model.region", {"kind": "Face", "value": "0"}),
             ("bottom", "model.region", {"kind": "Face", "value": "1"}),
             ("f", "model.feature", {"operation": "Midsurface", "name": "Mid"}),
             ("sec", "model.section", {"kind": "Shell", "thickness": 2}),
             ("mesh", "model.mesh", {"size": 5})]
    edges = [("base", "sketch", "part", "profile"), ("part", "part", "top", "source"),
             ("part", "part", "bottom", "source"), ("part", "part", "f", "part"), ("top", "region", "f", "region"),
             ("bottom", "region", "f", "opposite"), ("f", "part", "sec", "target"), ("sec", "part", "mesh", "target")]
    out, bad = script(plate, edges)
    assert not bad, bad
    text = out["script"]
    assert "part.AssignMidsurfaceRegion(cellList=part.cells[:])" in text and "part.OffsetFaces(" in text
    assert "section_targets = part.faces[:]" in text and "elemCode=S4R" in text
    log, stdout = run_in_fake_cae(text, tmp_path)
    assert "failed" not in stdout, stdout
    renamed = [c["kwargs"]["toName"] for c in calls(log, "changeKey")]
    assert "Mid-step1" in renamed and "Mid" in renamed
    _, bad = script(plate[:5] + [("sec", "model.section", {"kind": "Solid"})], edges[:7])
    assert "shell part needs a Shell section" in bad["sec"]
    skin = [plate[0], plate[1], ("f", "model.feature", {"operation": "Solid to shell"})]
    out, bad = script(skin, [edges[0], edges[3]])
    assert not bad, bad
    assert "part.RemoveCells(cellList=part.cells[:])" in out["script"]
    _, bad = script(skin[:2] + [("f", "model.feature", {"operation": "Midsurface"})], [edges[0], edges[3]])
    assert "Midsurface needs Region" in bad["f"]


def test_sketch_walls_offset_shapes_and_lines(tmp_path):
    from nodecode_fea.values import wall_items, _signed_area
    assert wall_items(("Circle", (0, 0), 10), 2, "Outside") == (("Circle", (0, 0), 12), ("Circle", (0, 0), 10))
    assert wall_items(("Circle", (0, 0), 10), 2, "Centred") == (("Circle", (0, 0), 11), ("Circle", (0, 0), 9))
    assert wall_items(("Rectangle", (0, 0), (100, 50), 5.0), 2, "Inside") == (
        ("Rectangle", (0, 0), (100, 50), 5.0), ("Rectangle", (2, 2), (98, 48), 3.0))
    with pytest.raises(ValueError, match="doesn't fit"):
        wall_items(("Circle", (0, 0), 1), 2, "Inside")
    # a C-channel from its centreline, 4 thick, centred: a closed band 4 wide everywhere
    (band,) = wall_items(("Profile", ((50, 0), (0, 0), (0, 100), (50, 100)), False), 4, "Centred")
    assert band[2] and len(band[1]) == 8 and abs(_signed_area(list(band[1]))) == pytest.approx(4 * (50 + 100 + 50))
    # a closed profile's wall goes out whichever way it was drawn
    square = ((0, 0), (10, 0), (10, 10), (0, 10))
    for pts in (square, square[::-1]):
        outer, inner = wall_items(("Profile", pts, True), 1, "Outside")
        assert abs(_signed_area(list(outer[1]))) == pytest.approx(144) and abs(_signed_area(list(inner[1]))) == 100
    # the Sketch node: a rectangular tube section, extruded
    nodes = [("s", "model.sketch", {"width": 100, "height": 50, "wall": 3, "wall_side": "Inside"}),
             ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 200})]
    out, bad = script(nodes, [("s", "sketch", "p", "profile")])
    assert not bad, bad
    assert "s.rectangle(point1=(0.0, 0.0), point2=(100.0, 50.0))" in out["script"]
    assert "s.rectangle(point1=(3.0, 3.0), point2=(97.0, 47.0))" in out["script"]
    _, bad = script([("s", "model.sketch", {"shape": "Profile", "points": "0,0; 10,0; 10,10", "corner": 2,
                                            "wall": 1})], [])
    assert "sharp corners" in bad["s"]


def test_composite_shell_layup_with_a_fibre_direction(tmp_path):
    """Lamina material, Composite shell section from a ply table (symmetric), material orientation along a fibre
    direction (a datum csys with its x axis there)."""
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {}),
             ("sec", "model.section", {"behavior": "Lamina", "e1": 140000, "e2": 9000, "nu12": 0.3, "g12": 4600,
                                       "g13": 4600, "g23": 3000, "kind": "Composite shell",
                                       "plies": "0, 0.2; 45, 0.2; -45, 0.2; 90, 0.2", "symmetric": "Yes",
                                       "orientation": "1, 1, 0"})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "sec", "target")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    text = out["script"]
    assert "material.Elastic(type=LAMINA, table=((140000.0, 9000.0, 0.3, 4600.0, 4600.0, 3000.0),))" in text
    assert "m.CompositeShellSection(name='Section-1'" in text and "symmetric=True" in text
    assert "orientAngle=-45.0" in text and "plyName='Ply-4'" in text
    assert "point1=(1.0, 1.0, 0.0), point2=(-1.0, 1.0, 0.0)" in text
    log, stdout = run_in_fake_cae(text, tmp_path)
    assert "failed" not in stdout, stdout
    assert len(calls(log, "section.SectionLayer")) == 4 and calls(log, "CompositeShellSection")
    assert calls(log, "MaterialOrientation")
    # plies from a matrix input; what doesn't fit is said plainly
    out, bad = script(nodes[:2] + [("t", "input.value", {"type": "Text", "value": "0, 0.5; 90, 0.5"}),
                                   ("sec", "model.section", {"behavior": "Lamina", "kind": "Composite shell"})],
                      edges + [("t", "value", "sec", "plies")])
    assert not bad, bad
    assert out["script"].count("SectionLayer(") == 2
    solid = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude"})]
    _, bad = script(solid + [("sec", "model.section", {"behavior": "Lamina"})], edges)
    assert "Lamina is for shells" in bad["sec"]
    out, bad = script(solid + [("sec", "model.section", {"behavior": "Engineering constants",
                                                          "orientation": "0, 0, 1"})], edges)
    assert not bad, bad
    assert "type=ENGINEERING_CONSTANTS" in out["script"] and "point2=(0.0, 1.0, 0.0)" in out["script"]
    _, bad = script(nodes[:2] + [("sec", "model.section", {"kind": "Composite shell", "plies": "0, -1"})], edges)
    assert "positive thickness" in bad["sec"]


def test_one_graph_model_and_results_share_cae_by_focus(odb_path, tmp_path):
    """Model and results in one graph: the node clicked last owns CAE's current viewport. A model node: it is shown,
    the contour waits; a contour: it is drawn, the model is built but not shown."""
    nodes = [("s", "model.sketch", {}), ("p", "model.part", {}),
             ("o", "odb.open", {"path": odb_path}), ("u", "odb.field", {"variable": "U", "quantity": "Magnitude"}),
             ("c", "view.contour", {})]
    edges = [("s", "sketch", "p", "profile"), ("o", "odb", "u", "odb"), ("u", "field", "c", "field")]
    out, bad = script(nodes, edges, focus="p", tab="model")
    assert not bad, bad
    assert "BaseShell" in out["script"] and "Show Part" in out["script"]
    assert any("the current viewport shows the model node clicked last" in x for x in out["skipped"])
    out, bad = script(nodes, edges, focus="c", tab="model")
    assert "BaseShell" in out["script"] and "Show Part" not in out["script"]
    assert "Contour" in out["script"] and not out["skipped"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
