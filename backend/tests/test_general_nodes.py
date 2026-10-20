"""The general nodes that replace groups of narrower ones: Quantity, Reduce, Count, Over Time, Pick, Make Curve."""

import pytest

from nodecode_fea.engine import Engine
from nodecode_fea.nodes import REGISTRY

from .test_engine import MOCK, graph


def out(nodes, edges, node, port):
    res, inputs = Engine().evaluate(graph(nodes + [("sink", "list.length", {})], edges + [(node, port, "sink", "list")]))
    assert res[node]["status"] == "ok", res[node]
    return inputs["sink"]["list"]


ODB = [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"})]
ODB_E = [("odb", "odb", "s", "odb")]


def test_quantity_is_invariant_or_component():
    mises = out(ODB + [("q", "field.quantity", {"quantity": "Mises"}), ("old", "field.invariant", {})],
                ODB_E + [("s", "field", "q", "field"), ("s", "field", "old", "field")], "q", "field")
    old = out(ODB + [("old", "field.invariant", {})], ODB_E + [("s", "field", "old", "field")], "old", "field")
    assert (mises.values == old.values).all()
    s11 = out(ODB + [("q", "field.quantity", {"quantity": "S11"})], ODB_E + [("s", "field", "q", "field")], "q", "field")
    assert s11.origin["refinement"] == ("COMPONENT", "S11")


@pytest.mark.parametrize("source, port, op, kind", [
    ("field", "field", "Max", "field"), ("curve", "series", "Last", "curve"), ("list", "list", "Mean", "list")])
def test_reduce_takes_fields_curves_and_lists(source, port, op, kind):
    nodes = {"field": ODB + [("x", "field.quantity", {"quantity": "Mises"})],
             "curve": [("odb", "odb.open", {"path": MOCK}), ("x", "field.time", {"labels": "BEAM-1.1"})],
             "list": [("x", "loop.for", {"start": 1, "stop": 4, "step": 1})]}[source]
    edges = {"field": ODB_E + [("s", "field", "x", "field")], "curve": [("odb", "odb", "x", "odb")], "list": []}[source]
    xport = {"field": "field", "curve": "series", "list": "i"}[source]
    v = out(nodes + [("r", "math.reduce", {"op": op})], edges + [("x", xport, "r", "field")], "r", "value")
    assert type(v).__name__ == "Number"
    if source == "list":
        assert v.value == 2.5
    if source == "field":
        assert "location" in v.extra


def test_reduce_maps_over_several_curves_and_counts():
    nodes = [("odb", "odb.open", {"path": MOCK}), ("t", "field.time", {"labels": "1, 2, 3"}),
             ("r", "math.reduce", {"op": "Abs Max"}), ("n", "math.reduce", {"op": "Count"})]
    edges = [("odb", "odb", "t", "odb"), ("t", "series", "r", "field"), ("t", "series", "n", "field")]
    assert len(out(nodes, edges, "r", "value")) == 3
    assert out(nodes, edges, "n", "value").value == 3


def test_count_sizes_anything():
    n = out(ODB + [("c", "list.length", {})], ODB_E + [("s", "field", "c", "list")], "c", "value")
    assert n.value == 640


def test_over_time_each_and_reduced():
    base = [("odb", "odb.open", {"path": MOCK})]
    each = out(base + [("t", "field.time", {"labels": "1, 2"})], [("odb", "odb", "t", "odb")], "t", "series")
    assert len(each) == 2
    peak = out(base + [("t", "field.time", {"op": "Max", "variable": "S", "quantity": "Mises", "first": -3})],
               [("odb", "odb", "t", "odb")], "t", "series")
    assert len(peak.x) == 3


def test_pick_from_list():
    base = [("f", "loop.for", {"start": 10, "stop": 15, "step": 1})]
    assert out(base + [("p", "list.pick", {"which": "-1"})], [("f", "i", "p", "list")], "p", "list").value == 15
    assert [x.value for x in out(base + [("p", "list.pick", {"which": "::-2"})], [("f", "i", "p", "list")],
                                 "p", "list")] == [15, 13, 11]
    one = out(ODB + [("st", "odb.set", {"name": "TIP"}), ("p", "list.pick", {"which": "0:3"})],
              ODB_E + [("odb", "odb", "st", "odb"), ("st", "set", "p", "list")], "p", "list")
    assert type(one).__name__ == "EntitySet" and len(one.labels) == 3


def test_make_curve_from_curves_and_numbers():
    base = [("odb", "odb.open", {"path": MOCK}), ("h", "odb.history", {"region": "Node set FIXED", "output": "RF2"}),
            ("u", "field.time", {"labels": "BEAM-1.1"})]
    e = [("odb", "odb", "h", "odb"), ("odb", "odb", "u", "odb")]
    c = out(base + [("m", "series.make", {"yscale": -1})], e + [("h", "series", "m", "y"), ("u", "series", "m", "x")],
            "m", "series")
    assert type(c).__name__ == "Series"
    nums = [("f", "loop.for", {"start": 1, "stop": 3, "step": 1}), ("m", "series.make", {"name": "sq"})]
    c = out(nums, [("f", "i", "m", "y"), ("f", "i", "m", "x")], "m", "series")
    assert list(c.x) == [1, 2, 3] and c.name == "sq"


def test_replaced_nodes_are_hidden():
    for t in ("logic.compare", "field.where", "text.test", "field.invariant", "field.component", "series.reduce",
              "list.stats", "field.reduce", "field.probe", "field.over_frames", "list.item", "list.slice",
              "list.reverse", "list.split", "list.range", "series.xy", "list.to_series", "input.integer",
              "text.from", "text.to_number", "text.join"):
        assert REGISTRY[t].hidden, t
    assert not REGISTRY["list.length"].hidden


@pytest.mark.parametrize("extra, edges, curves", [
    ([("mx", "math.reduce", {"op": "Max"})], [("s", "field", "mx", "field"), ("mx", "value", "t", "set")], 1),
    ([("hot", "logic.test", {"op": ">", "value": "420"})], [("s", "field", "hot", "a"), ("hot", "kept", "t", "set")],
     None),
    ([("st", "odb.set", {"name": "ROOT_ELEMENTS"}), ("p", "list.pick", {"which": "0:3"})],
     [("odb", "odb", "st", "odb"), ("st", "set", "p", "list"), ("p", "list", "t", "set")], 3),
])
def test_over_time_where_takes_nodes_from_anywhere(extra, edges, curves):
    base = [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S", "quantity": "Mises"})]
    got = out(base + extra + [("t", "field.time", {"variable": "S", "quantity": "Mises"})],
              [("odb", "odb", "s", "odb"), ("odb", "odb", "t", "odb")] + edges, "t", "series")
    n = 1 if not isinstance(got, list) else len(got)
    assert n == curves if curves else n > 1


def test_field_output_quantity():
    f = out([("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S", "quantity": "Mises"})],
            [("odb", "odb", "s", "odb")], "s", "field")
    assert f.values.shape[1] == 1 and f.origin["refinement"] == ("INVARIANT", "Mises")


@pytest.mark.parametrize("extra, edges, params, kind, n", [
    ([], [], {}, "Series", None),
    ([], [], {"mode": "At the points"}, "list", 2),
    ([("m", "input.value", {"type": "List / matrix", "value": "[[100, 0, 0], [50, 0, 0], [0, 0, 0]]"})],
     [("m", "value", "p", "points")], {"mode": "At the points"}, "list", 3),
    ([("t", "odb.set", {"name": "TIP"})], [("odb", "odb", "t", "odb"), ("t", "set", "p", "points")],
     {"mode": "At the points", "method": "Nearest node"}, "list", 25),
    ([("m", "input.value", {"type": "List / matrix", "value": "[[0, 0, 0], [100, 0, 0]]"})],
     [("m", "value", "p", "points")], {}, "Series", None),
])
def test_sample_field(extra, edges, params, kind, n):
    base = [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "U", "quantity": "U2"})]
    got = out(base + extra + [("p", "field.path", params)],
              [("odb", "odb", "s", "odb"), ("s", "field", "p", "field")] + edges, "p", "series")
    if kind == "Series":
        assert type(got).__name__ == "Series"
    else:
        assert isinstance(got, list) and len(got) == n


def test_field_output_combines_frames():
    base = [("odb", "odb.open", {"path": MOCK})]

    def field(**params):
        return out(base + [("s", "odb.field", {"variable": "U", "quantity": "U2", **params})],
                   [("odb", "odb", "s", "odb")], "s", "field")

    import numpy as np
    last, first = field(), field(frame=0)
    assert np.allclose(field(combine="Min over frames").values, np.minimum(last.values, 0))  # U2 goes down
    assert np.allclose(field(combine="Change (last - first)").values, last.values - first.values)
    mean = field(combine="Mean over frames", first=5, last=6).values
    assert np.allclose(mean, (field(frame=5).values + field(frame=6).values) / 2)
    assert field(combine="Abs max over frames").origin["combine"] == "Abs max over frames"
