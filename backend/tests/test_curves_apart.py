"""Taking Over Time results apart: points of a curve (Pick), X/Y values (Make Curve), the nodes/elements behind the
curves (Over Time's Nodes/elements) and the whole result as a table (Formula, Use inputs: As tables)."""

import numpy as np
import pytest

from nodecode_fea.nodes import AS_TABLES, REGISTRY

from .test_engine import MOCK
from .test_general_nodes import out

ODB = [("odb", "odb.open", {"path": MOCK})]
CURVES = ODB + [("t", "field.time", {"labels": "1, 2, 3"})]           # U2 at three nodes: a list of curves
ONE = ODB + [("t", "field.time", {"labels": "2"})]                     # one curve
ELEMS = ODB + [("t", "field.time", {"variable": "S", "quantity": "Mises", "labels": "1:4"})]
E = [("odb", "odb", "t", "odb")]


def curves(nodes=CURVES):
    return out(nodes, E, "t", "series")


# ------------------------------------------------------------------ 1. Pick from List on one curve: points
def test_pick_one_point_of_a_curve_is_its_y_value():
    c = curves(ONE)
    last = out(ONE + [("p", "list.pick", {"which": "-1"})], E + [("t", "series", "p", "list")], "p", "list")
    assert type(last).__name__ == "Number"
    assert last.value == pytest.approx(c.y[-1])
    assert last.extra["x"] == pytest.approx(c.x[-1]) and last.extra["location"]["label"] == 2
    first = out(ONE + [("p", "list.pick", {"which": "0"})], E + [("t", "series", "p", "list")], "p", "list")
    assert first.value == pytest.approx(c.y[0])


def test_pick_a_range_of_points_is_a_shorter_curve():
    c = curves(ONE)
    part = out(ONE + [("p", "list.pick", {"which": "0:5"})], E + [("t", "series", "p", "list")], "p", "list")
    assert type(part).__name__ == "Series"
    assert np.allclose(part.x, c.x[:5]) and np.allclose(part.y, c.y[:5])
    some = out(ONE + [("p", "list.pick", {"which": "0, -1"})], E + [("t", "series", "p", "list")], "p", "list")
    assert np.allclose(some.y, c.y[[0, -1]])


def test_pick_on_a_list_of_curves_still_picks_curves_and_count_counts_points():
    cs = curves()
    second = out(CURVES + [("p", "list.pick", {"which": "1"})], E + [("t", "series", "p", "list")], "p", "list")
    assert type(second).__name__ == "Series" and np.allclose(second.y, cs[1].y)
    n = out(ONE + [("n", "list.length", {})], E + [("t", "series", "n", "list")], "n", "value")
    assert n.value == len(curves(ONE).x)
    r = out(ONE + [("n", "math.reduce", {"op": "Count"})], E + [("t", "series", "n", "field")], "n", "value")
    assert r.value == len(curves(ONE).x)


# ------------------------------------------------------------------ 2. X / Y values on Make Curve
def test_make_curve_takes_one_curve_apart():
    c = curves(ONE)
    nodes, edges = ONE + [("m", "series.make", {})], E + [("t", "series", "m", "y")]
    xs, ys = out(nodes, edges, "m", "x"), out(nodes, edges, "m", "y")
    assert [n.value for n in xs] == pytest.approx(list(c.x))
    assert [n.value for n in ys] == pytest.approx(list(c.y))


def test_make_curve_gives_one_list_per_curve():
    cs = curves()
    nodes, edges = CURVES + [("m", "series.make", {})], E + [("t", "series", "m", "y")]
    ys = out(nodes, edges, "m", "y")
    assert len(ys) == 3 and all(isinstance(r, list) for r in ys)
    assert [n.value for n in ys[2]] == pytest.approx(list(cs[2].y))
    # One list each is a matrix: a Formula takes it as rows = curves.
    m = out(nodes + [("f", "math.formula", {"expr": "a[:, -1]"})], edges + [("m", "y", "f", "a")], "f", "value")
    assert [n.value for n in m] == pytest.approx([c.y[-1] for c in cs])


def test_make_curve_from_numbers_also_gives_x_and_y():
    nodes = [("r", "loop.for", {"start": 1, "stop": 4, "step": 1}), ("m", "series.make", {})]
    xs = out(nodes, [("r", "i", "m", "y")], "m", "x")
    assert [n.value for n in xs] == [0, 1, 2, 3]


# ------------------------------------------------------------------ 3. Over Time's Nodes/elements
def test_over_time_gives_the_labels_behind_its_curves_in_order():
    s = out(CURVES, E, "t", "set")
    assert s.kind == "node" and list(s.labels) == [1, 2, 3]
    els = out(ELEMS, E, "t", "set")
    assert els.kind == "element" and list(els.labels) == [1, 2, 3, 4]
    n = out(ELEMS + [("n", "list.length", {})], E + [("t", "set", "n", "list")], "n", "value")
    assert n.value == 4


def test_over_time_labels_wire_into_sets_and_formulas():
    nodes = ELEMS + [("u", "set.op", {"op": "Union"}), ("f", "math.formula", {"expr": "a * 10"})]
    edges = E + [("t", "set", "u", "a"), ("t", "set", "u", "b"), ("t", "set", "f", "a")]
    labels = out(nodes, edges, "f", "value")
    assert [n.value for n in labels] == [10, 20, 30, 40]
    assert sorted(out(nodes, edges, "u", "result").labels.tolist()) == [1, 2, 3, 4]


def test_over_time_reduced_curve_gives_the_members_it_reduced():
    nodes = ODB + [("t", "field.time", {"variable": "S", "quantity": "Mises", "op": "Max"})]
    s = out(nodes, E, "t", "set")
    assert s.kind == "element" and len(s.labels) > 4


def test_a_curve_stands_for_its_node_wherever_sets_are_accepted():
    # Pick one curve of Over Time and use it as Where of another Over Time.
    nodes = CURVES + [("p", "list.pick", {"which": "1"}), ("t2", "field.time", {"variable": "U", "quantity": "U1"})]
    edges = E + [("t", "series", "p", "list"), ("p", "list", "t2", "set"), ("odb", "odb", "t2", "odb")]
    c = out(nodes, edges, "t2", "series")
    assert c.extra["location"]["label"] == 2 and "U1" in c.name


# ------------------------------------------------------------------ 4. Formula: tables as the data panel shows them
MISES = ODB + [("s", "odb.field", {"variable": "S"}), ("q", "field.quantity", {"quantity": "Mises"})]
MISES_E = [("odb", "odb", "s", "odb"), ("s", "field", "q", "field")]


def formula(expr, nodes, edges, src, port="series", **params):
    return out(nodes + [("f", "math.formula", {"expr": expr, **params})], edges + [(src, port, "f", "a")],
               "f", "value")


def table(expr, nodes=CURVES, **params):
    return formula(expr, nodes, E, "t", **params)


def values(v):
    return [n.value for n in v]


def test_field_columns_are_the_ones_shown_without_any_setting():
    # The data panel shows Instance, Element, S Mises: a[:, 0], a[:, 1], a[:, 2] in a Formula, as plain lists.
    mises = out(MISES, MISES_E, "q", "field")
    col2 = formula("a[:, 2]", MISES, MISES_E, "q", "field")
    assert isinstance(col2, list) and values(col2) == pytest.approx(list(mises.values[:, 0]))
    assert values(formula("a[:, 2] / 2", MISES, MISES_E, "q", "field")) == pytest.approx(list(mises.values[:, 0] / 2))
    assert values(formula("a[:, 1]", MISES, MISES_E, "q", "field")) == list(mises.labels)
    inst = formula("a[:, 0]", MISES, MISES_E, "q", "field")
    assert type(inst[0]).__name__ == "Text" and inst[0].value == str(mises.instances[0])
    assert formula("a[0, 2]", MISES, MISES_E, "q", "field").value == pytest.approx(mises.values[0, 0])
    block = formula("a[:3, 1:]", MISES, MISES_E, "q", "field")  # a matrix: 3 rows × (Element, S Mises)
    assert len(block) == 3 and [n.value for n in block[0]] == pytest.approx([mises.labels[0], mises.values[0, 0]])
    # Plain values still work as before: a field stays a field.
    assert np.allclose(formula("a / 2", MISES, MISES_E, "q", "field").values[:, 0], mises.values[:, 0] / 2)


def test_a_column_shows_as_just_its_values():
    from nodecode_fea.engine import Engine

    from .test_engine import graph
    res, _ = Engine().evaluate(graph(MISES + [("f", "math.formula", {"expr": "a[:, 2]"})],
                                     MISES_E + [("q", "field", "f", "a")]))
    assert res["f"]["display"]["columns"] == ["#", "Value"]


def test_display_columns_match_the_formula_table():
    from nodecode_fea.engine import Engine

    from .test_engine import graph
    res, _ = Engine().evaluate(graph(MISES, MISES_E))
    d = res["q"]["display"]
    assert d["columns"][:2] == ["Instance", "Element"] and d["indexed"]
    mises = out(MISES, MISES_E, "q", "field")
    top = d["rowIndex"][0]  # rows are sorted by value: the first shown row is a[top]
    assert d["rows"][0][2] == pytest.approx(formula(f"a[{top}, 2]", MISES, MISES_E, "q", "field").value)
    assert d["rows"][0][2] == pytest.approx(float(np.nanmax(mises.values)))


def test_tensor_field_table_has_all_components():
    nodes, edges = ODB + [("s", "odb.field", {"variable": "S"})], [("odb", "odb", "s", "odb")]
    s = out(nodes, edges, "s", "field")
    assert values(formula("a[:, 2]", nodes, edges, "s", "field")) == pytest.approx(list(s.values[:, 0]))
    comps = formula("a[:, 2:]", nodes, edges, "s", "field")
    assert len(comps) == len(s.labels) and len(comps[0]) == len(s.components)


def test_curves_table_is_instance_node_then_frames():
    cs = curves()
    ys = np.array([c.y for c in cs])
    assert values(table("a[:, 2]")) == pytest.approx(list(ys[:, 0]))  # every node at the first frame
    assert values(table("a[:, 1]")) == [1, 2, 3]  # the node labels
    assert values(table("a[0, 2:]")) == pytest.approx(list(ys[0]))  # the first node's values over the frames
    assert values(table("np.max(a[:, 2:], axis=0)")) == pytest.approx(list(ys.max(axis=0)))
    tr = table("a[:, 2:].T")
    assert len(tr) == ys.shape[1] and len(tr[0]) == 3  # a matrix: frames × nodes
    assert table("t[-1]", use=AS_TABLES).value == pytest.approx(cs[0].x[-1])


def test_per_node_values_still_know_their_node():
    nodes = CURVES + [("f", "math.formula", {"expr": "np.max(np.abs(a[:, 2:]), axis=1)"}),
                      ("r", "math.reduce", {"op": "Max"})]
    edges = E + [("t", "series", "f", "a"), ("f", "value", "r", "field")]
    r = out(nodes, edges, "r", "value")
    assert r.value == pytest.approx(max(np.abs(c.y).max() for c in curves()))
    assert "location" in r.extra


def test_chart_display_offers_the_same_table():
    from nodecode_fea.engine import Engine

    from .test_engine import graph
    res, _ = Engine().evaluate(graph(CURVES, E))
    t = res["t"]["display"]["table"]
    assert t["columns"][:2] == ["Instance", "Node"] and len(t["rows"]) == 3 and t["rows"][1][1] == 2


def test_bad_index_explains_the_table():
    from nodecode_fea.engine import Engine

    from .test_engine import graph
    res, _ = Engine().evaluate(graph(CURVES + [("f", "math.formula", {"expr": "a[:, 2, 5]"})],
                                     E + [("t", "series", "f", "a")]))
    msg = res["f"]["message"]
    assert "[0] Instance" in msg and "[1] Node" in msg and "a[row, column]" in msg


def test_whole_table_maths_says_which_columns_are_names():
    from nodecode_fea.engine import Engine

    from .test_engine import graph
    res, _ = Engine().evaluate(graph(MISES + [("f", "math.formula", {"expr": "a * 2", "use": AS_TABLES})],
                                     MISES_E + [("q", "field", "f", "a")]))
    assert "a[:, 2]" in res["f"]["message"]


def test_values_stays_the_default_and_old_settings_still_load():
    p = {x.name: x for x in REGISTRY["math.formula"].params}["use"]
    assert p.default == "Values (one by one)"
    per_curve = table("-a")
    assert len(per_curve) == 3 and np.allclose(per_curve[0].y, -curves()[0].y)
    assert len(table("a[:, 2]", use="As tables (rows × columns)")) == 3
    assert len(table("-a", use="One by one")) == 3


def test_whole_table_and_row_slices_work_like_python():
    mises = out(MISES, MISES_E, "q", "field")
    whole = formula("a[:, :]", MISES, MISES_E, "q", "field")
    assert len(whole) == len(mises.labels) and len(whole[0]) == 3
    assert whole[0][0].value == str(mises.instances[0]) and whole[0][1].value == mises.labels[0]
    assert whole[0][2].value == pytest.approx(mises.values[0, 0])
    assert values(formula("a[2:, 1]", MISES, MISES_E, "q", "field")) == list(mises.labels[2:])
    assert len(formula("a[:3, :]", MISES, MISES_E, "q", "field")) == 3


def test_two_indices_on_a_list_say_it_is_one_column():
    from nodecode_fea.engine import Engine

    from .test_engine import graph
    nodes = MISES + [("f", "math.formula", {"expr": "a[:, 2]"}), ("g", "math.formula", {"expr": "a[2:, 1]"})]
    edges = MISES_E + [("q", "field", "f", "a"), ("f", "value", "g", "a")]
    res, _ = Engine().evaluate(graph(nodes, edges))
    assert "list of" in res["g"]["message"] and "a[2:]" in res["g"]["message"]
