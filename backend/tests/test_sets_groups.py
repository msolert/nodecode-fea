"""Set Operation (topology and set algebra) and Display Group."""

import numpy as np
import pytest

from nodecode_fea.cae_export import build_script
from nodecode_fea.engine import Engine
from nodecode_fea.mesh import surface_for
from nodecode_fea.sources import open_source

from .test_engine import MOCK, graph


def result(nodes, edges, node="op", port="result"):
    res, inputs = Engine().evaluate(graph(nodes + [("sink", "list.length", {})], edges + [(node, port, "sink", "list")]))
    assert res[node]["status"] == "ok", res[node]
    return inputs["sink"]["list"]


BASE = [("odb", "odb.open", {"path": MOCK})]


def op(name, a_node, extra_nodes=(), b=None):
    nodes = BASE + list(extra_nodes) + [("op", "set.op", {"op": name})]
    edges = [("odb", "odb", "op", "odb")] + [(n[0], "set", "op", "a") for n in extra_nodes[:1]]
    if b:
        edges.append((b, "set", "op", "b"))
    edges += [("odb", "odb", n[0], "odb") for n in extra_nodes]
    return result(nodes, edges)


def test_nodes_of_elements_and_back():
    root = [("r", "odb.set", {"name": "ROOT_ELEMENTS"})]
    nodes = op("Nodes of elements", "r", root)
    assert nodes.kind == "node" and len(nodes.labels) > 0
    one_el = [("e", "odb.set", {"kind": "element", "labels": "BEAM-1.1"})]
    corners = op("Nodes of elements", "e", one_el)
    assert len(corners.labels) == 8  # a hex


def test_elements_touching_any_all_and_inside():
    one = [("n", "odb.set", {"labels": "BEAM-1.1"})]
    touching = op("Elements touching any node", "n", one)
    assert len(touching.labels) >= 1
    # the 8 corner nodes of element 1: exactly one element has all of them, and only it lies inside them
    corners = [("e", "odb.set", {"kind": "element", "labels": "BEAM-1.1"})]
    nodes = op("Nodes of elements", "e", corners)
    labels = ", ".join(f"BEAM-1.{l}" for l in nodes.labels)
    all_of = op("Elements touching all nodes", "n", [("n", "odb.set", {"labels": labels})])
    inside = op("Elements inside (all nodes)", "n", [("n", "odb.set", {"labels": labels})])
    assert list(all_of.labels) == [1] and list(inside.labels) == [1]
    neighbours = op("Neighbour elements", "e", corners)
    assert 1 not in neighbours.labels and len(neighbours.labels) >= 1


def test_union_intersection_difference():
    sets = [("a", "odb.set", {"labels": "BEAM-1.1:5"}), ("b", "odb.set", {"labels": "BEAM-1.4:8"})]
    def run(name):
        return list(result(BASE + sets + [("op", "set.op", {"op": name})],
                           [("odb", "odb", "a", "odb"), ("odb", "odb", "b", "odb"), ("a", "set", "op", "a"),
                            ("b", "set", "op", "b")]).labels)
    assert run("Union") == [1, 2, 3, 4, 5, 6, 7, 8]
    assert run("Intersection") == [4, 5]
    assert run("Difference") == [1, 2, 3]


def test_field_on_set_replaces_filter_by_set():
    nodes = BASE + [("s", "odb.field", {"variable": "S"}), ("t", "odb.set", {"name": "ROOT_ELEMENTS"}),
                    ("op", "set.op", {"op": "Field on set"}), ("old", "field.filter", {})]
    edges = [("odb", "odb", "s", "odb"), ("odb", "odb", "t", "odb"), ("s", "field", "op", "a"),
             ("t", "set", "op", "b"), ("s", "field", "old", "field"), ("t", "set", "old", "set")]
    new = result(nodes, edges)
    old = result(nodes, edges, "old", "field")
    assert (new.labels == old.labels).all()


def test_display_group_in_editor_and_cae():
    nodes = BASE + [("s", "odb.field", {"variable": "S"}), ("q", "field.quantity", {"quantity": "Mises"}),
                    ("hot", "logic.test", {"op": ">", "value": "300"}),
                    ("g", "view.group", {"name": "Hot spots", "show": "Show only this group"})]
    edges = [("odb", "odb", "s", "odb"), ("s", "field", "q", "field"), ("q", "field", "hot", "a"),
             ("hot", "kept", "g", "members")]
    g = graph(nodes, edges)
    res, inputs = Engine().evaluate(g)
    assert res["g"]["status"] == "ok", res["g"]
    assert res["g"]["display"]["kind"] == "contour" and "Hot spots" in res["g"]["display"]["title"]
    out = build_script(g, res, inputs)
    assert out["contents"][0].startswith("Display group 'Hot spots'")
    assert "visibleDisplayGroups=(dg,)" in out["script"]


def test_an_empty_display_group_says_so():
    """Nothing passing a Compare (e.g. no element above yield) is an empty group, not an error."""
    nodes = BASE + [("s", "odb.field", {"variable": "S", "quantity": "Mises"}),
                    ("hot", "logic.test", {"op": ">", "value": "1e9"}), ("g", "view.group", {"name": "Yielded"})]
    edges = [("odb", "odb", "s", "odb"), ("s", "field", "hot", "a"), ("hot", "kept", "g", "members")]
    res, _ = Engine().evaluate(graph(nodes, edges))
    assert res["g"]["status"] == "ok", res["g"]
    assert ["Members", "0 elements"] in res["g"]["display"]["items"]


def test_display_group_rejects_mixed_kinds():
    nodes = BASE + [("a", "odb.set", {"name": "TIP"}), ("b", "odb.set", {"name": "ROOT_ELEMENTS"}),
                    ("g", "view.group", {})]
    edges = [("odb", "odb", "a", "odb"), ("odb", "odb", "b", "odb"), ("a", "set", "g", "members"),
             ("b", "set", "g", "members")]
    res, _ = Engine().evaluate(graph(nodes, edges))
    assert res["g"]["status"] == "error" and "one Display Group" in res["g"]["message"]
