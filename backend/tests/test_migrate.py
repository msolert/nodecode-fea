"""Saved graphs with replaced nodes become the general nodes and give the same results."""

import numpy as np
import pytest

from nodecode_fea.engine import Engine
from nodecode_fea.migrate import check_rules, migrate_graph
from nodecode_fea.nodes import REGISTRY

from .test_engine import MOCK, graph


def test_every_replaced_node_has_a_rule():
    assert check_rules() == []


def same_result(nodes, edges, node, port):
    """Evaluate a graph before and after migration; the value reaching a List Length sink must match."""
    g = graph(nodes + [("sink", "list.length", {})], edges + [(node, port, "sink", "list")])
    new, notes = migrate_graph(g)
    assert notes
    assert not any(REGISTRY[n["type"]].hidden for n in new["nodes"]), [n["type"] for n in new["nodes"]]
    before = Engine().evaluate(g)
    after = Engine().evaluate(new)
    for res, _ in (before, after):
        assert all(r["status"] == "ok" for r in res.values()), {k: r["message"] for k, r in res.items() if r["message"]}
    return before[1]["sink"]["list"], after[1]["sink"]["list"]


def values(v):
    if hasattr(v, "values") and hasattr(v, "labels"):
        return np.asarray(v.values, dtype=float).ravel().tolist()
    if hasattr(v, "y"):
        return list(v.y)
    if isinstance(v, list):
        return [values(x) for x in v]
    if hasattr(v, "labels"):
        return list(v.labels)
    return getattr(v, "value", v)


ODB = [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"})]
ODB_E = [("odb", "odb", "s", "odb")]


@pytest.mark.parametrize("nodes, edges, node, port", [
    (ODB + [("i", "field.invariant", {"invariant": "Mises"}), ("w", "field.where", {"op": ">", "value": 300})],
     ODB_E + [("s", "field", "i", "field"), ("i", "field", "w", "field")], "w", "field"),
    (ODB + [("i", "field.invariant", {}), ("r", "field.reduce", {"op": "Max"})],
     ODB_E + [("s", "field", "i", "field"), ("i", "field", "r", "field")], "r", "value"),
    (ODB + [("c", "field.component", {"component": "S11"}), ("x", "field.expression", {"expr": "x * a"}),
            ("n", "math.number", {"value": 2})],
     ODB_E + [("s", "field", "c", "field"), ("c", "field", "x", "field"), ("n", "value", "x", "a")], "x", "field"),
    ([("odb", "odb.open", {"path": MOCK}), ("p", "field.probe", {"variable": "U", "quantity": "U2", "labels": "1, 2"}),
      ("e", "series.expression", {"expr": "-y"}), ("r", "series.reduce", {"op": "Final"})],
     [("odb", "odb", "p", "odb"), ("p", "series", "e", "series"), ("e", "series", "r", "series")], "r", "value"),
    ([("r", "list.range", {"start": 1, "stop": 5, "step": 1}), ("it", "list.item", {"index": -1})],
     [("r", "list", "it", "list")], "it", "item"),
    ([("r", "list.range", {"start": 1, "stop": 5, "step": 1}), ("a", "loop.accumulate", {"op": "Sum"})],
     [("r", "list", "a", "values")], "a", "total"),
    ([("r", "list.range", {"start": 1, "stop": 5, "step": 1}), ("f", "list.filter", {"op": ">", "value": 2})],
     [("r", "list", "f", "list")], "f", "list"),
    ([("a", "math.number", {"value": 3}), ("b", "math.number", {"value": 4}),
      ("c", "logic.compare", {"op": "<"}), ("g", "logic.gate", {"op": "Not a"})],
     [("a", "value", "c", "a"), ("b", "value", "c", "b"), ("c", "result", "g", "a")], "g", "result"),
    ([("t", "text.value", {"text": "Job-12"}), ("u", "text.value", {"text": "x"}), ("j", "text.join", {"sep": "_"})],
     [("t", "text", "j", "a"), ("u", "text", "j", "b")], "j", "text"),
    ([("odb", "odb.open", {"path": MOCK}), ("st", "odb.steps", {}), ("fr", "odb.frames", {"every": 3})],
     [("odb", "odb", "st", "odb"), ("odb", "odb", "fr", "odb")], "fr", "frames"),
    (ODB + [("t", "odb.set", {"name": "ROOT_ELEMENTS"}), ("f", "field.filter", {})],
     ODB_E + [("odb", "odb", "t", "odb"), ("s", "field", "f", "field"), ("t", "set", "f", "set")], "f", "field"),
    ([("odb", "odb.open", {"path": MOCK}), ("pk", "cae.pick", {"labels": "BEAM-1.1, 2"})],
     [("odb", "odb", "pk", "odb")], "pk", "set"),
])
def test_migrated_graph_gives_the_same_result(nodes, edges, node, port):
    before, after = same_result(nodes, edges, node, port)
    assert values(before) == values(after)


def test_wired_settings_follow_renames():
    g = graph([("n", "math.number", {"value": 2}), ("r", "list.range", {"start": 0, "stop": 5, "step": 1}),
               ("it", "list.item", {})], [("r", "list", "it", "list"), ("n", "value", "it", "param:index")])
    new, _ = migrate_graph(g)
    assert any(e["targetHandle"] == "param:which" for e in new["edges"])
    res, _ = Engine().evaluate(new)
    assert res["it"]["status"] == "ok" and res["it"]["summary"].startswith("2")


def test_editor_shaped_graph(tmp_path):
    """The editor's nodes keep type/params under data and edges carry ids."""
    g = {"nodes": [{"id": "w", "type": "op", "position": {"x": 1, "y": 2},
                    "data": {"type": "field.where", "params": {"op": ">", "value": 5}, "name": "Hot"}}],
         "edges": [{"id": "x", "source": "w", "sourceHandle": "field", "target": "z", "targetHandle": "field"}]}
    new, notes = migrate_graph(g)
    d = new["nodes"][0]
    assert d["type"] == "op" and d["position"] == {"x": 1, "y": 2} and d["data"]["name"] == "Hot"
    assert d["data"]["type"] == "logic.test" and d["data"]["params"]["value"] == "5"
    assert new["edges"][0]["sourceHandle"] == "kept"
