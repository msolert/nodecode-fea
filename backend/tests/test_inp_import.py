"""Open CAE model on an Abaqus input file (.inp): the reader's dump (recorded from real CAE 2025 reading
data/plate_assembly.inp) mapped to a graph whose Regions select the mesh by label, and the Region node's Mesh
labels, down to the CAE helpers that resolve them."""

import json
import string
import sys
from pathlib import Path

import pytest

from nodecode_fea.cae_export import HEADER, build_script
from nodecode_fea.cae_import import _ranges, graph_from_dump, payload
from nodecode_fea.cae_model.collect import collect
from nodecode_fea.cae_reader import ReadError, read_cae
from nodecode_fea.engine import Engine
from nodecode_fea.values import Part, Region

from .test_engine import graph

DATA = Path(__file__).parent / "data"


def recorded_dump() -> dict:
    """What cae_reader gave for data/plate_assembly.inp, pointing at the file where it is now."""
    text = (DATA / "inp_dump.json").read_text().replace("INP_PATH", json.dumps(str(DATA / "plate_assembly.inp"))[1:-1])
    return json.loads(text)


@pytest.fixture
def dump():
    return recorded_dump()


def nodes_of(g, kind):
    return {n["data"].get("name"): n for n in g["nodes"] if n["data"]["type"] == kind}


def wired(g, target, handle):
    return [e["source"] for e in g["edges"] if e["target"] == target and e["targetHandle"] == handle]


def test_an_input_file_becomes_a_graph_with_nothing_left_out(dump):
    out = graph_from_dump(dump)
    assert out["notes"] == [] and out["model"] == "plate_assembly"
    g = out["graph"]
    part = nodes_of(g, "model.part")["PLATE"]["data"]["params"]
    # the part comes from the file itself (not a copy written out), by its name in it
    assert part == {"name": "PLATE", "body": "Solid", "method": "Import CAD",
                    "file": str(DATA / "plate_assembly.inp"), "entry": "PLATE"}
    res, _ = Engine().evaluate(payload(g))
    assert {k: v.get("message") for k, v in res.items() if v["status"] != "ok"} == {}
    assert [n["data"]["params"]["name"] for n in nodes_of(g, "model.job").values()] == ["plate_assembly"]


def test_its_sets_and_surfaces_are_regions_by_mesh_label(dump):
    g = graph_from_dump(dump)["graph"]
    regions = {n["data"]["name"]: n["data"]["params"] for n in g["nodes"] if n["data"]["type"] == "model.region"}
    assert regions["LEFT"] == {"method": "Mesh labels", "mesh": "Elements", "labels": "1-2, 5-6", "create": "Set",
                               "name": "LEFT", "side": "Side 1"}
    assert regions["FIXED"]["mesh"] == "Nodes" and regions["FIXED"]["labels"] == "1, 6, 11, 16, 21, 26"
    assert regions["TOP"]["mesh"] == "Element faces" and regions["TOP"]["labels"] == "S2: 1-8"
    assert regions["TOP"]["create"] == "Surface"
    # assembly sets are made on the instance (its nodes)
    tip = next(n for n in g["nodes"] if n["data"].get("name") == "TIP")
    assert wired(g, tip["id"], "source") == [nodes_of(g, "model.instance")["PLATE-1"]["id"]]
    assert tip["data"]["params"]["labels"] == "5, 10, 15, 20, 25, 30"


def test_sections_on_element_sets_and_loads_on_node_sets_and_surfaces(dump):
    g = graph_from_dump(dump)["graph"]
    by_id = {n["id"]: n for n in g["nodes"]}
    sections = nodes_of(g, "model.section")
    for section, elset in (("Section-1-LEFT on PLATE", "LEFT"), ("Section-2-RIGHT on PLATE", "RIGHT")):
        assert [by_id[s]["data"]["name"] for s in wired(g, sections[section]["id"], "target")] == [elset]
    assert sections["Section-2-RIGHT on PLATE"]["data"]["params"]["material"] == "ALU"
    conditions = nodes_of(g, "model.condition")
    on = {name: by_id[wired(g, c["id"], "region")[0]]["data"]["name"] for name, c in conditions.items()}
    assert on == {"Disp-BC-1": "FIXED", "Disp-BC-2": "TIP", "CFORCE-1": "CORNER", "SURFFORCE-1": "TOP"}
    assert conditions["SURFFORCE-1"]["data"]["params"]["magnitude"] == 2.5


def test_loads_on_sets_of_a_part_sectioned_by_parts_build_that_one_part(dump):
    """Regions on the bare part used by loads, and Instance after two sections on element sets: one CAE part."""
    g = payload(graph_from_dump(dump)["graph"])
    res, inputs = Engine().evaluate(g)
    objects, _, _ = collect(g, res, inputs)
    parts = [o for o in objects if isinstance(o["value"], Part)]
    assert [p["name"] for p in parts] == ["PLATE"] and len(parts[0]["value"].sections) == 2
    script = build_script(g, res, inputs)["script"]
    assert "('@labels', 'faces', (('S2', ((1, 8),)),))" in script
    assert "section_region = regionToolset.Region(**_region_kw(part, 'cells', _find_geometry(" in script


def test_label_ranges_are_written_as_in_an_input_file():
    assert _ranges([3, 1, 2, 7, 9, 8, 12]) == "1-3, 7-9, 12"
    assert _ranges([5]) == "5"


def region(params, body="Solid"):
    nodes = [("p", "model.part", {"name": "Mesh", "body": body, "method": "Import CAD",
                                  "file": str(DATA / "plate_assembly.inp")}),
             ("r", "model.region", {"method": "Mesh labels", **params})]
    res, inputs = Engine().evaluate(graph(nodes, [("p", "part", "r", "source")]))
    if res["r"]["status"] != "ok":
        return res["r"]["message"]
    return inputs.outputs["r"]["region"]


def test_mesh_labels_merge_ranges_and_take_the_kind_their_entities_are():
    r = region({"mesh": "Nodes", "labels": "7, 1-3 4\n10-12; 2"})
    assert isinstance(r, Region) and r.kind == "vertex"
    assert r.labels == ("@labels", "nodes", (("", ((1, 4), (7, 7), (10, 12))),))
    assert r.summary() == "8 mesh nodes by label"
    assert region({"mesh": "Elements", "labels": "1-4"}).kind == "cell"
    assert region({"mesh": "Elements", "labels": "1-4"}, body="Shell").kind == "face"  # sections need faces
    faces = region({"mesh": "Element faces", "labels": "s4: 3; S2: 1-2, 5", "create": "Surface", "name": "Top"})
    assert faces.kind == "face" and faces.form == "Surface"
    assert faces.labels[2] == (("S2", ((1, 2), (5, 5))), ("S4", ((3, 3),)))


@pytest.mark.parametrize("params, says", [
    ({"mesh": "Nodes", "labels": ""}, "needs labels"),
    ({"mesh": "Nodes", "labels": "1, x"}, "not x"),
    ({"mesh": "Nodes", "labels": "5-2"}, "must go up"),
    ({"mesh": "Element faces", "labels": "1-8"}, "by face"),
    ({"mesh": "Element faces", "labels": "S7: 1"}, "S1 to S6"),
    ({"mesh": "Nodes", "labels": "S2: 1"}, "numbers and ranges"),
    ({"mesh": "Nodes", "labels": "1", "create": "Surface"}, "can only make a Set"),
])
def test_mesh_labels_say_what_is_wrong(params, says):
    assert says in region(params)


def test_reading_needs_a_cae_or_an_input_file(tmp_path):
    with pytest.raises(ReadError, match=r"\(.cae\) or an Abaqus input file \(.inp\)"):
        read_cae(str(tmp_path / "model.odb"))
    with pytest.raises(ReadError, match="File not found"):
        read_cae(str(tmp_path / "missing.inp"))


# ------------------------------------------------------------------ the CAE helpers, on a tiny mesh
class _Seq(list):
    def sequenceFromLabels(self, labels):
        by = {x.label: x for x in self}
        return _Seq(by[k] for k in labels if k in by)


class _Node:
    def __init__(self, label):
        self.label = label


class _Face:
    def __init__(self, element, face):
        self.element, self.face = element, face

    def getElements(self):
        return [self.element]

    def getNodes(self):
        return [_Node(self.element.label * 10 + self.face)]


class _FaceName(int):  # CAE's face ids print as FACE1..FACE6
    def __str__(self):
        return f"FACE{int(self)}"


class _Element:
    def __init__(self, label):
        self.label = label

    def getElemFaces(self):
        return [_Face(self, _FaceName(k)) for k in range(1, 7)]


class _Owner:
    def __init__(self):
        self.nodes = _Seq(_Node(k) for k in range(1, 31))
        self.elements = _Seq(_Element(k) for k in range(1, 9))


@pytest.fixture(scope="module")
def helpers():
    sys.path.insert(0, str(Path(__file__).parent / "fake_abaqus"))
    try:
        keys = {f: "''" for _, f, _, _ in string.Formatter().parse(HEADER) if f}
        scope = {"__name__": "nocode_header"}
        exec(compile(HEADER.format(**keys), "header.py", "exec"), scope)
    finally:
        sys.path.remove(str(Path(__file__).parent / "fake_abaqus"))
    return scope


def test_cae_finds_nodes_elements_and_element_faces_by_label(helpers):
    owner = _Owner()
    find, kw = helpers["_find_geometry"], helpers["_region_kw"]
    nodes = find(owner.nodes, (("@labels", "nodes", (("", ((1, 3), (30, 30))),)),), 0.0, "R", owner=owner)
    assert [n.label for n in kw(owner, "vertices", nodes, "vertices")["nodes"]] == [1, 2, 3, 30]
    elements = find([], (("@labels", "elements", (("", ((2, 3),)),)),), 0.0, "R", owner=owner)
    assert [e.label for e in kw(owner, "cells", elements, "cells")["elements"]] == [2, 3]
    assert list(kw(owner, "faces", elements, "side1Faces")) == ["side1Elements"]  # shell elements' side
    faces = find([], (("@labels", "faces", (("S2", ((1, 2),)), ("S4", ((5, 5),)))),), 0.0, "R", owner=owner)
    surface = kw(owner, "faces", faces, "side1Faces")
    assert {k: [e.label for e in v] for k, v in surface.items()} == {"face2Elements": [1, 2], "face4Elements": [5]}


def test_cae_says_which_labels_the_mesh_lacks(helpers):
    owner = _Owner()
    with pytest.raises(ValueError, match="the mesh has no node 31, 40"):
        helpers["_find_geometry"]([], (("@labels", "nodes", (("", ((29, 31), (40, 40))),)),), 0.0, "R",
                                  owner=owner)
