"""Invariants follow what the ODB defines for each variable (e.g. strains have no Mises in Abaqus)."""

import pytest

from nodecode_fea.cae_export import build_script
from nodecode_fea.engine import Engine

from .test_engine import MOCK, graph, run


def field_graph(variable, extra_nodes, extra_edges):
    return ([("odb", "odb.open", {"path": MOCK}), ("f", "odb.field", {"variable": variable})] + extra_nodes,
            [("odb", "odb", "f", "odb")] + extra_edges)


def test_strain_offers_principals_only():
    nodes, edges = field_graph("E", [("inv", "field.invariant", {"invariant": "Max. Principal"})],
                               [("f", "field", "inv", "field")])
    res = run(nodes, edges)
    assert [o["value"] for o in res["inv"]["hints"]["invariant"]] == ["Max. Principal", "Mid. Principal",
                                                                      "Min. Principal"]
    assert res["inv"]["status"] == "ok"


@pytest.mark.parametrize("bad", ["Mises", "Tresca", "Pressure", "Magnitude"])
def test_strain_rejects_invariants_abaqus_lacks(bad):
    nodes, edges = field_graph("E", [("inv", "field.invariant", {"invariant": bad})], [("f", "field", "inv", "field")])
    res = run(nodes, edges)
    assert res["inv"]["status"] == "error"
    assert f"doesn't define '{bad}' for E" in res["inv"]["message"]
    assert "Max. Principal" in res["inv"]["message"]


def test_stress_and_displacement_invariants():
    res = run(*field_graph("S", [("inv", "field.invariant", {})], [("f", "field", "inv", "field")]))
    assert "Mises" in [o["value"] for o in res["inv"]["hints"]["invariant"]]
    assert "Magnitude" not in [o["value"] for o in res["inv"]["hints"]["invariant"]]
    res = run(*field_graph("U", [("inv", "field.invariant", {"invariant": "Magnitude"})], [("f", "field", "inv", "field")]))
    assert [o["value"] for o in res["inv"]["hints"]["invariant"]] == ["Magnitude"]
    assert res["inv"]["status"] == "ok"


def test_scalar_has_no_invariants():
    res = run(*field_graph("PEEQ", [("inv", "field.invariant", {"invariant": "Mises"})], [("f", "field", "inv", "field")]))
    assert res["inv"]["hints"]["invariant"] == []
    assert "no invariants" in res["inv"]["message"] and "component" in res["inv"]["message"]


def test_time_nodes_use_the_same_rules():
    res = run(
        [("odb", "odb.open", {"path": MOCK}),
         ("t", "field.over_frames", {"variable": "E", "quantity": "Mises", "op": "Max"}),
         ("ok", "field.over_frames", {"variable": "E", "quantity": "Max. Principal", "op": "Max"})],
        [("odb", "odb", "t", "odb"), ("odb", "odb", "ok", "odb")],
    )
    assert [o["value"] for o in res["t"]["hints"]["quantity"]][:3] == ["Max. Principal", "Mid. Principal",
                                                                       "Min. Principal"]
    assert "Mises" not in [o["value"] for o in res["t"]["hints"]["quantity"]]
    assert res["t"]["status"] == "error" and "doesn't define 'Mises'" in res["t"]["message"]
    assert res["ok"]["status"] == "ok"
