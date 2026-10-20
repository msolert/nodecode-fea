import pytest

from .test_engine import MOCK, run

BASE = [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
        ("m", "field.invariant", {"invariant": "Mises"})]
BASE_E = [("odb", "odb", "s", "odb"), ("s", "field", "m", "field")]


def test_merge_reduces_into_contour_and_stats():
    res = run(
        BASE + [("max", "field.reduce", {"op": "Max"}), ("min", "field.reduce", {"op": "Min"}),
                ("mean", "field.reduce", {"op": "Mean"}), ("merge", "list.merge", {}),
                ("stats", "list.stats", {"op": "Min"}), ("c", "view.contour", {}), ("v", "view.value", {})],
        BASE_E + [("m", "field", "max", "field"), ("m", "field", "min", "field"), ("m", "field", "mean", "field"),
                  ("max", "value", "merge", "a"), ("min", "value", "merge", "b"), ("mean", "value", "merge", "c"),
                  ("merge", "list", "stats", "list"), ("merge", "list", "c", "markers"),
                  ("merge", "list", "v", "value"), ("m", "field", "c", "field")],
    )
    assert res["merge"]["status"] == "ok", res["merge"]
    assert res["merge"]["summary"].startswith("3 numbers")
    assert len(res["c"]["display"]["markers"]) == 2 and len(res["c"]["display"]["notes"]) == 1
    assert res["stats"]["display"]["where"].startswith("element BEAM-1.")
    table = res["v"]["display"]
    assert table["kind"] == "table" and [r[0] for r in table["rows"]] == [0, 1, 2]


def test_list_into_single_input_runs_once_per_item():
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("tip", "odb.set", {"name": "TIP"}),
         ("p", "field.probe", {"variable": "U", "quantity": "U2", "labels": ""}),
         ("peak", "series.reduce", {"op": "Min"}),
         ("sf", "math.expression", {"expr": "a * 2"})],
        [("odb", "odb", "tip", "odb"), ("odb", "odb", "p", "odb"), ("tip", "set", "p", "set"),
         ("p", "series", "peak", "series"), ("peak", "value", "sf", "a")],
    )
    assert res["p"]["summary"].startswith("25 curves")  # 5 x 5 nodes on the tip face
    assert res["peak"]["summary"].startswith("25 numbers")
    vals = [r[2] for r in res["sf"]["display"]["rows"]]
    assert len(vals) == 25 and all(v < 0 for v in vals)


def test_over_frames_each_and_limits():
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("root", "odb.set", {"name": "ROOT_ELEMENTS"}),
         ("each", "field.over_frames", {"variable": "S", "quantity": "Mises", "op": "Each (one curve per item)"}),
         ("all", "field.over_frames", {"variable": "S", "quantity": "Mises", "op": "Each (one curve per item)"})],
        [("odb", "odb", "root", "odb"), ("odb", "odb", "each", "odb"), ("root", "set", "each", "set"),
         ("odb", "odb", "all", "odb")],
    )
    assert len(res["each"]["display"]["series"]) == 80
    assert res["all"]["status"] == "error" and "limit" in res["all"]["message"]


def test_list_item_slice_sort_and_type_errors():
    nums = [(f"n{i}", "math.number", {"value": v, "label": f"N{i}"}) for i, v in enumerate([3, 1, 2])]
    res = run(
        nums + [("merge", "list.merge", {}), ("sort", "list.sort", {"order": "Ascending"}),
                ("first", "list.item", {"index": 0}), ("last2", "list.slice", {"range": "-2:"}),
                ("len", "list.length", {}), ("bad", "field.invariant", {}), ("big", "list.item", {"index": 9}),
                ("chart", "view.chart", {})],
        [("n0", "value", "merge", "a"), ("n1", "value", "merge", "b"), ("n2", "value", "merge", "c"),
         ("merge", "list", "sort", "list"), ("sort", "list", "first", "list"), ("sort", "list", "last2", "list"),
         ("last2", "list", "len", "list"), ("first", "item", "bad", "field"), ("merge", "list", "big", "list"),
         ("merge", "list", "chart", "series")],
    )
    assert res["first"]["display"]["value"] == 1
    assert res["len"]["display"]["value"] == 2
    assert res["bad"]["status"] == "error" and "expects field" in res["bad"]["message"]
    assert res["big"]["status"] == "error" and "out of range" in res["big"]["message"]
    assert res["chart"]["status"] == "error"


def test_output_node_rejects_mapped_list():
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("s1", "odb.field", {"variable": "S", "frame": 3}),
         ("s2", "odb.field", {"variable": "S"}), ("merge", "list.merge", {}), ("t", "view.table", {})],
        [("odb", "odb", "s1", "odb"), ("odb", "odb", "s2", "odb"), ("s1", "field", "merge", "a"),
         ("s2", "field", "merge", "b"), ("merge", "list", "t", "field")],
    )
    assert res["t"]["status"] == "error" and "List Item" in res["t"]["message"]


@pytest.mark.parametrize("text", ["1:x", "1:2:3:4"])
def test_bad_slice(text):
    from nodecode_fea.nodes import _parse_slice
    with pytest.raises(ValueError):
        _parse_slice(text)


def test_list_nodes_on_sets_and_fields():
    """A set is a list of its members: List Item 1 on a node set is the 2nd node, as a one-member set."""
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("tip", "odb.set", {"name": "TIP"}),
         ("item", "list.item", {"index": 1}), ("last", "list.item", {"index": -1}),
         ("len", "list.length", {}), ("first3", "list.slice", {"range": "0:3"}),
         ("probe", "field.probe", {"variable": "U", "quantity": "U2", "labels": ""}),
         ("split", "list.split", {}), ("each", "field.probe", {"variable": "U", "quantity": "U2", "labels": ""}),
         ("u", "odb.field", {"variable": "U"}), ("c", "field.component", {"component": "U2"}),
         ("sorted", "list.sort", {"order": "Ascending"}), ("lowest", "list.item", {"index": 0})],
        [("odb", "odb", "tip", "odb"), ("tip", "set", "item", "list"), ("tip", "set", "last", "list"),
         ("tip", "set", "len", "list"), ("tip", "set", "first3", "list"),
         ("odb", "odb", "probe", "odb"), ("item", "item", "probe", "set"),
         ("tip", "set", "split", "list"), ("odb", "odb", "each", "odb"), ("split", "list", "each", "set"),
         ("odb", "odb", "u", "odb"), ("u", "field", "c", "field"), ("c", "field", "sorted", "list"),
         ("sorted", "list", "lowest", "list")],
    )
    from nodecode_fea.sources import open_source
    tip = open_source(MOCK).set("TIP")
    assert res["item"]["display"]["kind"] == "info"
    assert res["item"]["summary"] == "1 nodes"
    assert f"BEAM-1.{tip['labels'][1]}" in res["item"]["display"]["title"]
    assert f"BEAM-1.{tip['labels'][-1]}" in res["last"]["display"]["title"]
    assert res["len"]["display"]["value"] == len(tip["labels"])
    assert res["first3"]["summary"] == "3 nodes"
    # The one-member set drives a probe: one curve, for that node.
    assert res["probe"]["display"]["series"][0]["name"] == f"U2 @ BEAM-1.{tip['labels'][1]}"
    # Split + probe: the probe runs once per member.
    assert res["each"]["summary"].startswith(f"{len(tip['labels'])} curves")
    # Field rows sort by value: the lowest U2 is a tip node (largest downward deflection).
    assert res["lowest"]["display"]["total"] == 1
    assert res["lowest"]["display"]["rows"][0][2] == pytest.approx(min(r[2] for r in res["c"]["display"]["allRows"]))
