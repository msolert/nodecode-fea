"""Step, frame and ODB can come from wires anywhere they are needed."""

import pytest

from nodecode_fea.engine import Engine

from .test_engine import MOCK, graph


def run(nodes, edges):
    return Engine().evaluate(graph(nodes, edges))


def test_frame_range_on_over_time_nodes():
    res, inputs = run(
        [("odb", "odb.open", {"path": MOCK}),
         ("all", "field.over_frames", {"variable": "U", "quantity": "U2", "op": "Min"}),
         ("part", "field.over_frames", {"variable": "U", "quantity": "U2", "op": "Min", "first": 2, "last": 6,
                                        "every": 2}),
         ("p", "field.probe", {"variable": "U", "quantity": "U2", "labels": "1", "first": -3}),
         ("fr", "odb.frames", {"first": 1, "last": -2, "every": 3}),
         ("bad", "odb.frames", {"first": 5, "last": 2}),
         ("n", "list.length", {}), ("m", "list.length", {}), ("k", "list.length", {})],
        [("odb", "odb", "all", "odb"), ("odb", "odb", "part", "odb"), ("odb", "odb", "p", "odb"),
         ("odb", "odb", "fr", "odb"), ("odb", "odb", "bad", "odb"), ("part", "series", "n", "list"),
         ("p", "series", "m", "list"), ("fr", "frames", "k", "list")],
    )
    for k in ("all", "part", "p", "fr"):
        assert res[k]["status"] == "ok", (k, res[k])
    assert len(inputs["n"]["list"].x) == 3  # frames 2, 4, 6
    assert len(inputs["m"]["list"].x) == 3  # the last three frames
    nframes = len(Engine().evaluate(graph([("odb", "odb.open", {"path": MOCK}), ("f", "odb.frames", {}),
                                           ("c", "list.length", {})],
                                          [("odb", "odb", "f", "odb"), ("f", "frames", "c", "list")]))[1]["c"]["list"])
    want = list(range(1, nframes - 1, 3))
    if want[-1] != nframes - 2:
        want.append(nframes - 2)
    assert [int(f.value) for f in inputs["k"]["list"]] == want
    assert res["bad"]["status"] == "error" and "after To frame" in res["bad"]["message"]


def test_odb_inputs_take_a_path():
    res, inputs = run(
        [("path", "text.value", {"text": MOCK}), ("f", "odb.field", {"variable": "U"}),
         ("s", "odb.set", {"name": "TIP"}), ("bad", "text.value", {"text": "/nope/missing.odb"}),
         ("g", "odb.field", {"variable": "U"})],
        [("path", "text", "f", "odb"), ("path", "text", "s", "odb"), ("bad", "text", "g", "odb")],
    )
    assert res["f"]["status"] == "ok", res["f"]
    assert res["s"]["status"] == "ok", res["s"]
    assert res["g"]["status"] == "error" and res["g"]["message"].startswith("ODB:")


def test_list_of_paths_loops_without_open_odb():
    res, inputs = run(
        [("paths", "text.split", {"sep": ","}), ("t", "text.value", {"text": f"{MOCK},{MOCK}&load=2000"}),
         ("f", "odb.field", {"variable": "U"}), ("n", "list.length", {})],
        [("t", "text", "paths", "text"), ("paths", "list", "f", "odb"), ("f", "field", "n", "list")],
    )
    assert res["f"]["status"] == "ok", res["f"]
    assert len(inputs["n"]["list"]) == 2
