"""The editor's 3D preview of parts, instances and regions (the model3d display), built without CAE."""

from nodecode_fea.engine import Engine

from .test_engine import graph


def run(nodes, edges):
    res, _ = Engine().evaluate(graph(nodes, edges))
    bad = {k: v["message"] for k, v in res.items() if v["status"] != "ok"}
    assert not bad, bad
    return res


BASE = [("m", "model.model", {"name": "M"}), ("s", "model.sketch", {}),
        ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10})]
BASE_EDGES = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile")]


def test_an_extruded_part_is_drawn_in_3d_from_its_profile():
    d = run(BASE, BASE_EDGES)["p"]["display"]
    assert d["kind"] == "model3d" and d["model"] == "M" and d["focus"] == ""
    (body,) = d["bodies"]
    assert body["method"] == "Extrude" and body["depth"] == 10 and len(body["loops"]) == 1 and body["steps"] == []


def test_a_planar_part_is_drawn_in_3d_too():
    nodes = BASE[:2] + [("p", "model.part", {"body": "Shell", "method": "Planar"})]
    d = run(nodes, BASE_EDGES)["p"]["display"]
    assert d["kind"] == "model3d" and d["bodies"][0]["method"] == "Planar"


def test_a_datum_is_shown_on_its_part():
    def datum(params, extra=(), edges=()):
        nodes = BASE + [("d", "model.datum", params), *extra]
        d = run(nodes, BASE_EDGES + [("p", "part", "d", "part"), *edges])["d"]["display"]
        assert d["kind"] == "model3d" and len(d["bodies"]) == 1
        (g,) = d["datums"]
        return g

    g = datum({"name": "Mid", "plane": "YZ", "offset": 25})
    assert g == {"name": "Mid", "kind": "plane", "origin": [25.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]}
    g = datum({"kind": "Principal axis", "axis": "Y"})
    assert g["kind"] == "axis" and g["direction"] == [0.0, 1.0, 0.0]
    pts = [("at", "input.value", {"type": "Text", "value": "0, 0, 0; 1, 0, 0; 0, 1, 0"})]
    g = datum({"kind": "Plane through 3 points"}, pts, [("at", "value", "d", "points")])
    assert g["kind"] == "plane" and g["normal"] == [0.0, 0.0, 1.0] and len(g["points"]) == 3
    pts = [("at", "input.value", {"type": "Text", "value": "1, 2, 3"})]
    g = datum({"kind": "Point"}, pts, [("at", "value", "d", "points")])
    assert g == {"name": "Datum-1", "kind": "point", "origin": [1.0, 2.0, 3.0]}


def test_an_instance_is_drawn_where_it_is_placed():
    nodes = BASE + [("i", "model.instance", {"name": "A", "x": 5}), ("j", "model.instance", {"name": "B", "y": 7})]
    edges = BASE_EDGES + [("p", "part", "i", "part"), ("p", "part", "j", "part")]
    res = run(nodes, edges)
    a, b = res["i"]["display"], res["j"]["display"]
    assert a["focus"] == "A" and b["focus"] == "B"
    assert a["bodies"][0]["steps"] == [["move", [5.0, 0.0, 0.0]]]
    assert b["bodies"][0]["steps"] == [["move", [0.0, 7.0, 0.0]]]


def test_a_region_picked_at_points_shows_them_on_the_model():
    nodes = BASE + [("i", "model.instance", {}), ("at", "input.value", {"type": "Text", "value": "0, 25, 5"}),
                    ("f", "model.region", {"method": "Find at", "kind": "Face"})]
    edges = BASE_EDGES + [("p", "part", "i", "part"), ("i", "instance", "f", "source"), ("at", "value", "f", "points")]
    d = run(nodes, edges)["f"]["display"]
    assert d["kind"] == "model3d" and d["markers"] == [[0.0, 25.0, 5.0]]
