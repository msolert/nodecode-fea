"""Set Operation on model Regions: union / intersection / difference of faces, edges, vertices or cells on one Part
or Instance, resolved in CAE by entity and usable anywhere a Region is (named Set/Surface, loads, features)."""

import pytest

from .test_cae_export import calls, run_in_fake_cae
from .test_model import evaluate, script

BASE = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
        ("a", "model.region", {"kind": "Face", "value": "0, 1, 2"}),
        ("b", "model.region", {"kind": "Face", "value": "1, 3"})]
BASE_EDGES = [("s", "sketch", "p", "profile"), ("p", "part", "a", "source"), ("p", "part", "b", "source"),
              ("a", "region", "op", "a"), ("b", "region", "op", "b")]
NAMED = [("set", "model.region", {"create": "Set", "name": "Combined"})]
NAMED_EDGES = [("op", "result", "set", "source")]


@pytest.mark.parametrize("op, kept", [("Union", [0, 1, 2, 3]), ("Intersection", [1]), ("Difference", [0, 2])])
def test_regions_combine_into_one_named_set(tmp_path, op, kept):
    nodes = BASE + [("op", "set.op", {"op": op})] + NAMED
    _, res, inputs = evaluate(nodes, BASE_EDGES + NAMED_EDGES)
    assert res["op"]["status"] == "ok", res["op"]
    region = inputs.outputs["op"]["result"]
    assert region.kind == "face" and region.boolean[1] == op
    assert region.summary().startswith(op.lower() + " of (3 faces picked) and (2 faces picked)")
    assert inputs.outputs["set"]["region"].summary().endswith("as Set Combined")
    out, bad = script(nodes, BASE_EDGES + NAMED_EDGES)
    assert not bad, bad
    assert "'@bool'" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "Traceback" not in stdout, stdout
    (made,) = [c for c in calls(log, ".Set") if c["kwargs"].get("name") == "Combined"]
    faces = made["kwargs"]["faces"]  # the path the fake recorded: faces[i:i+1] for each kept face
    assert sorted(int(x.split(":")[0]) for x in faces.split("faces[")[2:]) == kept, faces
    assert f"{op.lower()} kept {len(kept)} geometr" in stdout


def test_an_empty_intersection_says_so(tmp_path):
    nodes = [n if n[0] != "b" else ("b", "model.region", {"kind": "Face", "value": "4"}) for n in BASE]
    nodes += [("op", "set.op", {"op": "Intersection"})] + NAMED
    out, bad = script(nodes, BASE_EDGES + NAMED_EDGES)
    assert not bad, bad
    _, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "the intersection of a and b is empty" in stdout


def test_a_list_in_a_is_its_union_first_and_results_chain():
    nodes = BASE + [("c", "model.region", {"kind": "Face", "value": "5"}), ("op", "set.op", {"op": "Union"}),
                    ("op2", "set.op", {"op": "Difference"})]
    edges = BASE_EDGES + [("p", "part", "c", "source"), ("op", "result", "op2", "a"), ("c", "region", "op2", "b")]
    _, res, inputs = evaluate(nodes, edges)
    assert res["op2"]["status"] == "ok", res["op2"]
    outer = inputs.outputs["op2"]["result"]
    assert outer.boolean[1] == "Difference" and outer.boolean[2][1][0][0] == "@bool"


def test_what_regions_cannot_combine():
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "a", "source"), ("p", "part", "i", "part"),
             ("i", "instance", "b", "source"), ("a", "region", "op", "a"), ("b", "region", "op", "b")]
    nodes = BASE[:3] + [("i", "model.instance", {}), ("b", "model.region", {"kind": "Face", "value": "1"}),
                        ("op", "set.op", {"op": "Union"})]
    _, res, _ = evaluate(nodes, edges)
    assert res["op"]["status"] == "error" and "same Part or Instance" in res["op"]["message"]
    nodes = BASE[:3] + [("b", "model.region", {"kind": "Edge", "value": "1"}), ("op", "set.op", {"op": "Union"})]
    _, res, _ = evaluate(nodes, BASE_EDGES)
    assert res["op"]["status"] == "error" and "same geometry" in res["op"]["message"]
    _, res, _ = evaluate(BASE + [("op", "set.op", {"op": "Nodes of elements"})], BASE_EDGES)
    assert res["op"]["status"] == "error" and "Union, Intersection or Difference" in res["op"]["message"]
