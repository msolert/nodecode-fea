"""The mesh CAE generated, shown in the editor's 3D preview (with a Region's elements highlighted)."""

import json

import pytest

from nodecode_fea import cae_mesh
from nodecode_fea.cae_export import build_script

from .test_cae_export import run_in_fake_cae
from .test_model import evaluate

NODES = [("s", "model.sketch", {}), ("p", "model.part", {"method": "Extrude"}),
         ("mesh", "model.mesh", {"size": 4}),
         ("r", "model.region", {"kind": "Face", "value": "0"})]
EDGES = [("s", "sketch", "p", "profile"), ("p", "part", "mesh", "target"), ("mesh", "part", "r", "source")]


@pytest.fixture(autouse=True)
def empty_cache():
    cae_mesh._meshes.clear()
    yield
    cae_mesh._meshes.clear()


def _keys(script):
    found = [ln.split("'")[1] for ln in script.splitlines() if ln.strip().startswith("_meshes['")]
    return list(dict.fromkeys(found))


def test_meshed_part_and_region_are_asked_once_then_cached(tmp_path):
    g, res, inputs = evaluate(NODES, EDGES)
    script = build_script(g, res, inputs)["script"]
    assert cae_mesh.MARKER in script
    assert "_part_mesh(mdb.models['NoCode-Model'].parts['Part-1'])" in script
    assert "_region_mesh(mdb.models['NoCode-Model'].parts['Part-1'], 'face'" in script
    part_key, region_key = _keys(script)
    log, stdout = run_in_fake_cae(script, tmp_path)
    assert "Traceback" not in stdout, stdout
    # what CAE answers: one quad on face 0 (highlighted), one more elsewhere
    nodes = {"1": [0, 0, 0], "2": [1, 0, 0], "3": [1, 1, 0], "4": [0, 1, 0], "5": [2, 0, 0], "6": [2, 1, 0]}
    answer = {part_key: {"nodes": nodes, "faces": [[1, 2, 3, 4], [2, 5, 6, 3]], "lines": [], "points": [],
                         "elements": 2},
              region_key: {"nodes": {k: nodes[k] for k in "1234"}, "faces": [[1, 2, 3, 4]], "lines": [],
                           "points": [], "elements": 1}}
    assert cae_mesh.ingest("noise\n" + cae_mesh.MARKER + json.dumps(answer))
    assert not cae_mesh.ingest(cae_mesh.MARKER + json.dumps(answer))  # nothing new
    assert cae_mesh.MARKER not in build_script(g, res, inputs)["script"]

    cae_mesh.attach(res, inputs)
    shown = res["mesh"]["display"]
    assert shown["kind"] == "model3d" and shown["mesh"]["elements"] == 2 and len(shown["mesh"]["faces"]) == 2
    assert "highlight" not in shown["mesh"]
    region = res["r"]["display"]["mesh"]
    assert region["highlight"]["elements"] == 1
    assert [[region["nodes"][i] for i in f] for f in region["highlight"]["faces"]] == [[[0, 0, 0], [1, 0, 0],
                                                                                         [1, 1, 0], [0, 1, 0]]]
    # the Part node before the Mesh shows its geometry, not the mesh made later
    assert "mesh" not in (res["p"]["display"] or {})


def test_a_mesh_on_a_region_highlights_it_and_a_changed_mesh_is_asked_again():
    nodes = NODES[:2] + [("r", "model.region", {"kind": "Edge", "value": "0"}),
                         ("mesh", "model.mesh", {"size": 4, "seed": "By number", "number": 6})]
    edges = [EDGES[0], ("p", "part", "r", "source"), ("r", "region", "mesh", "target")]
    g, res, inputs = evaluate(nodes, edges)
    script = build_script(g, res, inputs)["script"]
    assert "_region_mesh(mdb.models['NoCode-Model'].parts['Part-1'], 'edge'" in script
    part_key, region_key = _keys(script)
    nodes_xyz = {"1": [0, 0, 0], "2": [1, 0, 0], "3": [2, 0, 0]}
    cae_mesh.ingest(cae_mesh.MARKER + json.dumps({
        part_key: {"nodes": nodes_xyz, "faces": [], "lines": [[1, 2], [2, 3]], "points": [], "elements": 2},
        region_key: {"nodes": nodes_xyz, "faces": [], "lines": [[1, 2]], "points": [1, 2], "elements": 1}}))
    cae_mesh.attach(res, inputs)
    highlight = res["mesh"]["display"]["mesh"]["highlight"]
    assert len(highlight["lines"]) == 1 and len(highlight["points"]) == 2
    assert "mesh" not in (res["r"]["display"] or {})  # the Region node itself is on the geometry before meshing
    # another seed is another mesh: not the cached one
    nodes[3] = ("mesh", "model.mesh", {"size": 4, "seed": "By number", "number": 8})
    g, res, inputs = evaluate(nodes, edges)
    assert part_key not in _keys(build_script(g, res, inputs)["script"])
    assert len(_keys(build_script(g, res, inputs)["script"])) == 2
    cae_mesh.attach(res, inputs)
    assert "mesh" not in (res["mesh"]["display"] or {})


def test_unmeshed_parts_and_empty_answers_are_not_shown():
    g, res, inputs = evaluate(NODES[:2] + [("mesh", "model.mesh", {"generate": "No"})], EDGES[:2])
    assert cae_mesh.MARKER not in build_script(g, res, inputs)["script"]
    assert not cae_mesh.ingest(cae_mesh.MARKER + json.dumps({"k": {"nodes": {}, "faces": [], "elements": 0}}))


def test_a_region_of_faces_without_elements_highlights_nothing():
    # a midsurface part keeps its solid cell but meshes only the midsurface: CAE gives None for the solid faces'
    # elements, which the preview reads as nothing to highlight (not an error)
    class Face:
        def getElementFaces(self):
            return None

        def getElements(self):
            return None

    class Part:
        cells, elements = [object()], [object()]

    scope = {"_LIMIT": cae_mesh.LIMIT}
    exec(cae_mesh.CAE_CODE, scope)
    assert scope["_region_mesh"](Part(), "face", (Face(),)) == {
        "nodes": {}, "faces": [], "lines": [], "points": [], "elements": 0}


def test_too_big_mesh_says_so():
    g, res, inputs = evaluate(NODES[:3], EDGES[:2])
    (part_key,) = _keys(build_script(g, res, inputs)["script"])
    cae_mesh.ingest(cae_mesh.MARKER + json.dumps({part_key: {"too_big": 123456}}))
    cae_mesh.attach(res, inputs)
    assert res["mesh"]["display"]["mesh"] == {"too_big": 123456}


def test_a_region_on_an_instance_shows_the_instance_mesh_placed():
    nodes = NODES[:3] + [("i", "model.instance", {"x": 100}),
                         ("r", "model.region", {"kind": "Face", "value": "0"})]
    edges = EDGES[:2] + [("mesh", "part", "i", "part"), ("i", "instance", "r", "source")]
    g, res, inputs = evaluate(nodes, edges)
    assert res["r"]["status"] == "ok"
    script = build_script(g, res, inputs)["script"]
    inst = "mdb.models['NoCode-Model'].rootAssembly.instances['Part-1-1']"
    assert f"_part_mesh({inst})" in script and f"_region_mesh({inst}, 'face'" in script
    keys = _keys(script)
    xyz = {"1": [100, 0, 0], "2": [101, 0, 0], "3": [101, 1, 0], "4": [100, 1, 0]}
    answer = {k: {"nodes": xyz, "faces": [[1, 2, 3, 4]], "lines": [], "points": [], "elements": 1} for k in keys}
    cae_mesh.ingest(cae_mesh.MARKER + json.dumps(answer))
    cae_mesh.attach(res, inputs)
    shown = res["r"]["display"]
    assert shown["focus"] and shown["mesh"]["highlight"]["elements"] == 1
    assert res["i"]["display"]["mesh"]["elements"] == 1  # the Instance node too


def test_a_datum_on_a_meshed_part_is_drawn_on_its_mesh():
    nodes = NODES[:3] + [("d", "model.datum", {"plane": "XZ", "offset": 5})]
    edges = EDGES[:2] + [("mesh", "part", "d", "part")]
    g, res, inputs = evaluate(nodes, edges)
    (part_key,) = _keys(build_script(g, res, inputs)["script"])
    cae_mesh.ingest(cae_mesh.MARKER + json.dumps({part_key: {
        "nodes": {"1": [0, 0, 0], "2": [1, 0, 0], "3": [1, 1, 0]}, "faces": [[1, 2, 3]], "lines": [], "points": [],
        "elements": 1}}))
    cae_mesh.attach(res, inputs)
    shown = res["d"]["display"]
    assert shown["mesh"]["elements"] == 1 and shown["datums"][0]["normal"] == [0.0, 1.0, 0.0]
