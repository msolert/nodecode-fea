"""Open CAE model: a .cae read by cae_reader (a recorded dump, no Abaqus needed) mapped to a graph of existing nodes."""

import copy
import json
import math
from pathlib import Path

import numpy as np
import pytest

from nodecode_fea.cae_import import _euler, _loops, _material_params, graph_from_dump, payload
from nodecode_fea.engine import Engine
from nodecode_fea.nodes import REGISTRY

DUMP = Path(__file__).parent / "data" / "cae_dump.json"


@pytest.fixture
def dump(tmp_path):
    """The recorded dump, with the part files it names (ACIS, input files) where the test can make them."""
    text = DUMP.read_text().replace("IMPORT_DIR", json.dumps(str(tmp_path))[1:-1])
    d = json.loads(text)
    for p in d["model"]["parts"]:
        for key in ("sat", "inp"):
            if p.get(key):
                Path(p[key]).write_text("")
    return d


def by_name(graph, kind):
    return {n["data"].get("name"): n for n in graph["nodes"] if n["data"]["type"] == kind}


def wired(graph, target, handle):
    return [e["source"] for e in graph["edges"] if e["target"] == target and e["targetHandle"] == handle]


def test_the_graph_uses_only_existing_visible_nodes_and_evaluates(dump):
    out = graph_from_dump(dump)
    g = out["graph"]
    for n in g["nodes"]:
        assert n["type"] == "op" and n["data"]["type"] in REGISTRY
        assert not REGISTRY[n["data"]["type"]].hidden
    for e in g["edges"]:
        assert e["id"] == f"{e['source']}.{e['sourceHandle']}-{e['target']}.{e['targetHandle']}"
    res, _ = Engine().evaluate(payload(g))
    bad = {k: v.get("message") for k, v in res.items() if v["status"] != "ok"}
    assert not bad


def test_sketched_parts_are_sketch_and_part_nodes(dump):
    g = graph_from_dump(dump)["graph"]
    parts = by_name(g, "model.part")
    plate = parts["Plate"]["data"]["params"]
    assert plate == {"name": "Plate", "body": "Solid", "method": "Extrude", "depth": 20.0}
    ring = parts["Ring"]["data"]["params"]
    assert ring["method"] == "Revolve" and ring["angle"] == 270.0
    assert parts["Tri"]["data"]["params"]["method"] == "Planar"
    # the plate: a closed profile with the hole's circle added into it
    profile = g["nodes"][[n["id"] for n in g["nodes"]].index(wired(g, parts["Plate"]["id"], "profile")[0])]
    assert profile["data"]["params"]["shape"] == "Profile" and profile["data"]["params"]["closed"] == "Closed"
    hole = [n for n in g["nodes"] if n["id"] in wired(g, profile["id"], "add")]
    assert [h["data"]["params"]["shape"] for h in hole] == ["Circle"]
    assert hole[0]["data"]["params"]["radius"] == pytest.approx(10.0)
    res, _ = Engine().evaluate(payload(g))
    assert res[parts["Plate"]["id"]]["status"] == "ok"


def test_a_part_with_more_features_is_imported_from_its_geometry_and_said_so(dump):
    out = graph_from_dump(dump)
    block = by_name(out["graph"], "model.part")["Block"]["data"]["params"]
    assert block["method"] == "Import CAD" and block["file"].endswith("Bracket-Block.sat")
    assert any(n.startswith("Part Block: imported as geometry") and "Round-1" in n for n in out["notes"])


def test_materials_and_sections_and_the_one_the_nodes_cant_hold(dump):
    out = graph_from_dump(dump)
    g = out["graph"]
    sections = {n["data"]["name"]: n for n in g["nodes"] if n["data"]["type"] == "model.section"}
    steel = sections["SteelSolid on Plate"]["data"]["params"]
    assert steel["material"] == "Steel" and steel["young"] == 210000.0 and steel["poisson"] == 0.3
    assert steel["plastic"] == "0, 250; 0.1, 300" and steel["density"] == 7.85e-9 and steel["kind"] == "Solid"
    assert sections["Skin on Tri"]["data"]["params"]["thickness"] == 2.0
    # Hot has thermal expansion: used from the .cae, so the graph builds a model of its own beside it
    hot = sections["HotSolid on Ring"]
    (existing,) = wired(g, hot["id"], "existing")
    picked = next(n for n in g["nodes"] if n["id"] == existing)["data"]["params"]
    assert picked["type"] == "Material in CAE" and picked["value"] == "Bracket|Hot"
    assert out["model"] == "Bracket-rebuilt"
    assert by_name(g, "model.model")["Bracket-rebuilt"]
    assert any("Open source.cae in Abaqus/CAE" in n and "material Hot (it has expansion)" in n for n in out["notes"])


def test_without_such_a_material_the_model_keeps_its_name(dump):
    d = copy.deepcopy(dump)
    for mat in d["model"]["materials"]:
        mat["behaviours"] = [b for b in mat["behaviours"] if b != "expansion"]
    out = graph_from_dump(d)
    assert out["model"] == "Bracket"
    assert not any(n["data"]["params"].get("type") == "Material in CAE" for n in out["graph"]["nodes"])


def test_instances_are_placed_where_they_are_in_cae(dump):
    """Each instance's chain of Instance nodes moves the part's vertices exactly where CAE has them."""
    out = graph_from_dump(dump)
    g = out["graph"]
    _, values = Engine().evaluate(payload(g))
    poses = {i["name"]: i for i in dump["model"]["instances"]}
    finals = {}
    for n in g["nodes"]:
        if n["data"]["type"] == "model.instance" and not any(
                e["source"] == n["id"] and e["targetHandle"] == "part" for e in g["edges"]
                if next(x for x in g["nodes"] if x["id"] == e["target"])["data"]["type"] == "model.instance"):
            finals[n["data"]["name"]] = values.outputs[n["id"]]["instance"]
    assert set(finals) == set(poses)
    turned = 0
    for name, inst in finals.items():
        steps = inst.steps
        R, t = np.eye(3), np.zeros(3)
        for st in steps:
            if st[0] == "rotate":
                i, j = {"X": (1, 2), "Y": (2, 0), "Z": (0, 1)}[st[1]]  # right-handed about the axis
                a = math.radians(st[2])
                c, s = math.cos(a), math.sin(a)
                M = np.eye(3)
                M[i, i], M[i, j], M[j, i], M[j, j] = c, -s, s, c
                R, t = M @ R, M @ t
                turned += 1
            elif st[0] == "move":
                t = t + np.array(st[1])
        assert np.allclose(R, poses[name]["rotation"], atol=1e-9), name
        assert np.allclose(t, poses[name]["translation"], atol=1e-6), name
    assert turned >= 4  # Plate-2 about z, Ring-1 about a skew axis (three turns)


def test_euler_angles_rebuild_any_rotation():
    rng = np.random.default_rng(1)
    for _ in range(50):
        q = rng.normal(size=4)
        q /= np.linalg.norm(q)
        w, x, y, z = q
        R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        a, b, c = map(math.radians, _euler(R.tolist()))
        Rx = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
        Ry = np.array([[math.cos(b), 0, math.sin(b)], [0, 1, 0], [-math.sin(b), 0, math.cos(b)]])
        Rz = np.array([[math.cos(c), -math.sin(c), 0], [math.sin(c), math.cos(c), 0], [0, 0, 1]])
        assert np.allclose(Rz @ Ry @ Rx, R, atol=1e-9)


def test_steps_outputs_conditions_and_tie(dump):
    out = graph_from_dump(dump)
    g = out["graph"]
    steps = by_name(g, "model.step")
    load = steps["Load"]["data"]["params"]
    assert load["kind"] == "Static" and load["time"] == 2.0 and load["nlgeom"] == "Yes" and load["increments"] == 500
    assert load["field"] == "RF, S, U"
    assert steps["Modes"]["data"]["params"] == {"name": "Modes", "kind": "Frequency", "modes": 5}
    assert wired(g, steps["Modes"]["id"], "after") == [steps["Load"]["id"]]
    conds = by_name(g, "model.condition")
    assert conds["Clamp"]["data"]["params"]["kind"] == "Fixed"
    pull = conds["Pull"]["data"]["params"]
    assert (pull["u1"], pull["u2"], pull["ur3"]) == ("0.5", "Free", "0")
    push = conds["Push"]["data"]["params"]
    assert push["kind"] == "Pressure" and push["magnitude"] == 5.0 and push["amplitude"] == "0, 0; 1, 1; 2, 0.5"
    tip = conds["Tip"]["data"]["params"]
    assert tip["kind"] == "Concentrated force" and (tip["x"], tip["y"], tip["z"]) == (0.0, -10.0, 0.0)
    assert conds["Weight"]["data"]["params"]["z"] == -9810.0 and not wired(g, conds["Weight"]["id"], "region")
    for c in conds.values():
        assert wired(g, c["id"], "step") == [steps["Load"]["id"]]
    tie = by_name(g, "model.interaction")["Glue"]
    assert tie["data"]["params"]["kind"] == "Tie" and tie["data"]["params"]["adjust"] == "No"
    regions = {n["id"]: n for n in g["nodes"] if n["data"]["type"] == "model.region"}
    assert regions[wired(g, tie["id"], "master")[0]]["data"]["params"]["create"] == "Surface"
    assert regions[wired(g, tie["id"], "master")[0]]["data"]["params"]["name"] == "PlateBack"
    # a named set keeps its name; a picked one is a plain selection
    assert regions[wired(g, conds["Clamp"]["id"], "region")[0]]["data"]["params"]["name"] == "Fix"
    assert regions[wired(g, conds["Pull"]["id"], "region")[0]]["data"]["params"]["create"] == "Selection"
    # what's only noted, never guessed
    assert any(n.startswith("Boundary condition Clamp: made in Initial") for n in out["notes"])
    job = by_name(g, "model.job")["BracketJob"]["data"]["params"]
    assert job == {"name": "BracketJob", "cpus": 1, "memory": 80}


def test_explicit_steps_are_imported_with_their_mass_scaling(dump):
    initial = dump["model"]["steps"][0]
    scaling = {"objective": "SEMI_AUTOMATIC", "type": "BELOW_MIN", "dt": 1e-06, "occurs": "AT_BEGINNING",
               "region": "('', 0, 0, 0)"}
    dump["model"]["steps"] = [initial,
                              {"name": "Hit", "type": "ExplicitDynamicsStep", "previous": "Initial", "timePeriod": 0.002,
                               "nlgeom": "ON", "scaleFactor": 0.9, "timeIncrementationMethod": "AUTOMATIC_GLOBAL",
                               "massScaling": [scaling]},
                              {"name": "Settle", "type": "ExplicitDynamicsStep", "previous": "Hit",
                               "timePeriod": 0.01, "nlgeom": "ON", "scaleFactor": 1.0,
                               "massScaling": [dict(scaling, occurs="THROUGHOUT_STEP")]}]
    out = graph_from_dump(dump)
    steps = by_name(out["graph"], "model.step")
    hit = steps["Hit"]["data"]["params"]
    assert (hit["kind"], hit["time"], hit["nlgeom"], hit["scale"], hit["mass_dt"]) == (
        "Dynamic explicit", 0.002, "Yes", 0.9, 1e-06)
    assert "mass_dt" not in steps["Settle"]["data"]["params"]
    assert any(n.startswith("Step Settle: mass scaling") for n in out["notes"])
    res, _ = Engine().evaluate(payload(out["graph"]))
    assert res[steps["Settle"]["id"]]["status"] == "ok", res[steps["Settle"]["id"]]


def test_regions_find_their_geometry_at_points_on_it(dump):
    g = graph_from_dump(dump)["graph"]
    _, values = Engine().evaluate(payload(g))
    for n in g["nodes"]:
        if n["data"]["type"] != "model.region":
            continue
        region = values.outputs[n["id"]]["region"]
        assert n["data"]["params"]["method"] == "Find at" and region.points
        assert n["data"]["params"]["tolerance"] == (0.0 if region.kind == "cell" else 1e-3)


def test_the_mesh_is_rebuilt_from_its_seed_controls_and_element_type(dump):
    g = graph_from_dump(dump)["graph"]
    mesh = by_name(g, "model.mesh")["Mesh Plate"]["data"]["params"]
    assert mesh == {"size": 5.0, "deviation": 0.1, "minimum": 0.1, "generate": "Yes", "technique": "Sweep",
                    "shape": "Hex", "element": "C3D8R"}
    assert len(by_name(g, "model.mesh")) == 1  # the other parts aren't seeded


def test_the_notes_are_in_the_graph_too(dump):
    out = graph_from_dump(dump)
    note = next(n for n in out["graph"]["nodes"] if n["id"] == "cae-notes")
    assert note["data"]["type"] == "input.value" and note["data"]["params"]["type"] == "Text"
    assert note["data"]["params"]["value"] == "\n".join(out["notes"])


def test_a_revolve_the_other_way_is_imported_not_guessed(dump):
    d = copy.deepcopy(dump)
    ring = next(p for p in d["model"]["parts"] if p["name"] == "Ring")
    ring["vertices"] = [[x, y, -z] for x, y, z in ring["vertices"]]  # flipRevolveDirection
    out = graph_from_dump(d)
    assert by_name(out["graph"], "model.part")["Ring"]["data"]["params"]["method"] == "Import CAD"


def test_sketches_with_arcs_or_branches_are_not_drawn():
    arc = [{"type": "ARC", "points": [[1, 0], [0, 1], [0, 0]]}]
    assert "arc" in _loops(arc)[2]
    branch = [{"type": "LINE", "points": p} for p in ([[0, 0], [1, 0]], [[1, 0], [2, 0]], [[1, 0], [1, 1]])]
    assert "branch" in _loops(branch)[2]
    square = [{"type": "LINE", "points": p} for p in ([[0, 0], [1, 0]], [[1, 1], [1, 0]], [[1, 1], [0, 1]],
                                                       [[0, 1], [0, 0]])]
    ((closed, pts),), circles, why = _loops(square)
    assert closed and len(pts) == 4 and not circles and not why


def test_unsupported_materials_say_why():
    base = {"name": "M", "behaviours": ["elastic"], "elastic": {"type": "ISOTROPIC", "table": [[1.0, 0.3]]}}
    assert _material_params(base)[1] == ""
    hot = dict(base, elastic={"type": "ISOTROPIC", "table": [[1.0, 0.3]], "temperature": True})
    assert "temperature" in _material_params(hot)[1]
    hyper = {"name": "R", "behaviours": ["hyperelastic"],
             "hyperelastic": {"type": "NEO_HOOKE", "testData": False, "table": [[0.5, 0.01]]}}
    assert _material_params(hyper)[0]["behavior"] == "Neo-Hooke"


def test_the_endpoint_returns_the_graph_or_the_models_to_choose_from(dump, monkeypatch):
    from fastapi.testclient import TestClient

    from nodecode_fea import app as app_module, cae_reader

    seen = []

    def fake(path, model=""):
        seen.append((path, model))
        return dump if model or len(dump["models"]) == 1 else {"models": ["A", "B"]}

    monkeypatch.setattr(cae_reader, "read_cae", fake)
    client = TestClient(app_module.app)
    r = client.post("/api/open-cae", json={"path": "x.cae"}).json()
    assert r["model"] == "Bracket-rebuilt" and r["notes"] and r["graph"]["nodes"][0]["id"] == "cae-notes"
    dump["models"] = ["A", "B"]
    assert client.post("/api/open-cae", json={"path": "x.cae"}).json() == {"models": ["A", "B"]}
    assert client.post("/api/open-cae", json={"path": "x.cae", "model": "Bracket"}).json()["model"]
    assert seen[-1] == ("x.cae", "Bracket")

    def broken(path, model=""):
        raise cae_reader.ReadError("Choose an Abaqus/CAE model database (.cae)")

    monkeypatch.setattr(cae_reader, "read_cae", broken)
    r = client.post("/api/open-cae", json={"path": "x.txt"})
    assert r.status_code == 400 and "model database" in r.json()["detail"]


def test_numbers_keep_the_digits_cae_gave(dump):
    g = graph_from_dump(dump)["graph"]
    texts = " ".join(str(n["data"]["params"].get("value", "")) for n in g["nodes"])
    assert "13.333333" in texts and "59.915434" in texts
