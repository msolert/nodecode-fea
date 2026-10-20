"""Field from Sets: user-made fields on node/element sets, usable like any field."""

import numpy as np
import pytest

from nodecode_fea.engine import Engine

from .test_engine import MOCK, graph

RP = MOCK + ("&" if "?" in MOCK else "?") + "rp=1"


def evaluate(nodes, edges, sink):
    res, inputs = Engine().evaluate(graph(nodes + [("n", "list.length", {})], edges + [(sink, "field", "n", "list")]))
    return res, inputs["n"]["list"] if "n" in inputs else None


SETS = [("odb", "odb.open", {"path": MOCK}), ("fixed", "odb.set", {"name": "FIXED"}),
        ("tip", "odb.set", {"name": "TIP"})]
SETS_E = [("odb", "odb", "fixed", "odb"), ("odb", "odb", "tip", "odb")]


def test_value_per_set_only_on_the_sets():
    res, f = evaluate(SETS + [("c", "field.from_set", {"expr": "set * 10"})],
                      SETS_E + [("odb", "odb", "c", "odb"), ("fixed", "set", "c", "set"), ("tip", "set", "c", "set")], "c")
    assert res["c"]["status"] == "ok", res["c"]
    assert f.position == "NODAL" and len(f.labels) == 50
    assert sorted(set(f.values[:, 0])) == [10, 20]


def test_formula_of_position_over_the_whole_model():
    res, f = evaluate(SETS + [("c", "field.from_set", {"expr": "x", "rest": "0 elsewhere"})],
                      SETS_E + [("odb", "odb", "c", "odb"), ("tip", "set", "c", "set")], "c")
    assert res["c"]["status"] == "ok", res["c"]
    v = f.values[:, 0]
    assert len(v) > 50 and (v == 0).sum() == len(v) - 25 and v.max() == pytest.approx(100)


def test_element_set_with_a_value_input_and_contour():
    nodes = [("odb", "odb.open", {"path": MOCK}), ("root", "odb.set", {"name": "ROOT_ELEMENTS"}),
             ("five", "math.number", {"value": 5}), ("c", "field.from_set", {"expr": "a * 2"}),
             ("cp", "view.contour", {})]
    edges = [("odb", "odb", "root", "odb"), ("odb", "odb", "c", "odb"), ("root", "set", "c", "set"),
             ("five", "value", "c", "a"), ("c", "field", "cp", "field")]
    res, f = evaluate(nodes, edges, "c")
    assert res["c"]["status"] == "ok", res["c"]
    assert f.position == "ELEMENT" and set(f.values[:, 0]) == {10}
    assert res["cp"]["status"] == "ok", res["cp"]


def test_value_list_must_match_sets_or_members():
    nodes = SETS + [("vals", "list.range", {"start": 1, "stop": 3, "step": 1}), ("c", "field.from_set", {"expr": "a"})]
    edges = SETS_E + [("odb", "odb", "c", "odb"), ("tip", "set", "c", "set"), ("vals", "list", "c", "a")]
    res, _ = evaluate(nodes, edges, "c")
    assert res["c"]["status"] == "error" and "one per set" in res["c"]["message"]


def test_fields_from_sets_add_up_in_a_formula():
    nodes = SETS + [("a", "field.from_set", {"expr": "1", "rest": "0 elsewhere"}),
                    ("b", "field.from_set", {"expr": "2", "rest": "0 elsewhere"}),
                    ("sum", "math.formula", {"expr": "a + b"})]
    edges = SETS_E + [("odb", "odb", "a", "odb"), ("odb", "odb", "b", "odb"), ("fixed", "set", "a", "set"),
                      ("tip", "set", "b", "set"), ("a", "field", "sum", "a"), ("b", "field", "sum", "b")]
    res, inputs = Engine().evaluate(graph(nodes + [("n", "list.length", {})], edges + [("sum", "value", "n", "list")]))
    v = inputs["n"]["list"].values[:, 0]
    assert sorted(np.unique(v)) == [0, 1, 2] and (v == 1).sum() == 25 and (v == 2).sum() == 25


def test_mixed_node_and_element_sets_error():
    nodes = SETS + [("root", "odb.set", {"name": "ROOT_ELEMENTS"}), ("c", "field.from_set", {})]
    edges = SETS_E + [("odb", "odb", "root", "odb"), ("odb", "odb", "c", "odb"), ("tip", "set", "c", "set"),
                      ("root", "set", "c", "set")]
    res, _ = evaluate(nodes, edges, "c")
    assert res["c"]["status"] == "error" and "node and element" in res["c"]["message"]


def test_assembly_level_reference_point():
    nodes = [("odb", "odb.open", {"path": RP}), ("s", "odb.set", {"name": "TIP_AND_RP"}),
             ("c", "field.from_set", {"expr": "set"})]
    res, f = evaluate(nodes, [("odb", "odb", "s", "odb"), ("odb", "odb", "c", "odb"), ("s", "set", "c", "set")], "c")
    assert res["c"]["status"] == "ok", res["c"]
    assert "" in set(map(str, f.instances)) and len(f.labels) == 26
    _, whole = evaluate(nodes[:2] + [("c", "field.from_set", {"rest": "0 elsewhere"})],
                        [("odb", "odb", "s", "odb"), ("odb", "odb", "c", "odb"), ("s", "set", "c", "set")], "c")
    assert whole.values[:, 0].sum() == 26
