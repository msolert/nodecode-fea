"""The second round of merges: every old capability still reachable through the remaining nodes."""

import pytest

from nodecode_fea.cae_export import build_script
from nodecode_fea.engine import Engine
from nodecode_fea.nodes import REGISTRY

from .test_engine import MOCK, graph


def ev(nodes, edges):
    return Engine().evaluate(graph(nodes, edges))


def sink(nodes, edges, node, port):
    res, inputs = ev(nodes + [("sink", "list.length", {})], edges + [(node, port, "sink", "list")])
    assert res[node]["status"] == "ok", res[node]
    return inputs["sink"]["list"]


def test_if_filters_lists_and_sets():
    base = [("f", "loop.for", {"start": 1, "stop": 5, "step": 1}), ("c", "logic.test", {"op": ">", "value": "2"})]
    e = [("f", "i", "c", "a")]
    kept = sink(base + [("if", "logic.if", {})], e + [("c", "result", "if", "condition"), ("f", "i", "if", "then")],
                "if", "result")
    assert [k.value for k in kept] == [3, 4, 5]
    # a field's rows, kept where a per-row Yes/No list says so
    nodes = [("odb", "odb.open", {"path": MOCK}), ("st", "odb.set", {"name": "TIP"}),
             ("lab", "list.pick", {"which": "0:5"}), ("each", "loop.foreach", {}),
             ("yes", "input.value", {"type": "Yes/No", "value": "yes"}), ("if", "logic.if", {})]
    out = sink(nodes, [("odb", "odb", "st", "odb"), ("st", "set", "lab", "list"), ("lab", "list", "if", "then"),
                       ("lab", "list", "each", "list"), ("yes", "value", "if", "condition")], "if", "result")
    assert type(out).__name__ == "EntitySet" and len(out.labels) == 5


def test_reduce_running_replaces_accumulate():
    run = sink([("f", "loop.for", {"start": 1, "stop": 4, "step": 1}), ("r", "math.reduce", {"op": "Sum"})],
               [("f", "i", "r", "field")], "r", "running")
    assert [x.value for x in run] == [1, 3, 6, 10]
    prod = sink([("f", "loop.for", {"start": 1, "stop": 4, "step": 1}), ("r", "math.reduce", {"op": "Product"})],
                [("f", "i", "r", "field")], "r", "value")
    assert prod.value == 24


def test_set_takes_labels_or_odb_set():
    base = [("odb", "odb.open", {"path": MOCK})]
    typed = sink(base + [("s", "odb.set", {"labels": "BEAM-1.1, 2:4"})], [("odb", "odb", "s", "odb")], "s", "set")
    assert list(typed.labels) == [1, 2, 3, 4] and typed.kind == "node"
    named = sink(base + [("s", "odb.set", {"name": "TIP"})], [("odb", "odb", "s", "odb")], "s", "set")
    assert len(named.labels) == 25
    res, _ = ev(base + [("s", "odb.set", {"name": "TIP", "labels": "1"})], [("odb", "odb", "s", "odb")])
    assert res["s"]["status"] == "error"


@pytest.mark.parametrize("t, value, cls", [("Number", "2.5e3", "Number"), ("Text", "Job-1", "Text"),
                                           ("Yes/No", "No", "Bool")])
def test_input_node(t, value, cls):
    v = sink([("i", "input.value", {"type": t, "value": value})], [], "i", "value")
    assert type(v).__name__ == cls


@pytest.mark.parametrize("kind, value, cls, name", [
    ("Sketch in CAE", "Bridge Model|Web outline", "Sketch", "Web outline"),
    ("Part in CAE", "Bridge Model|Main girder", "Part", "Main girder"),
    ("Instance in CAE", "Bridge Model|Girder-1|Main girder", "Instance", "Girder-1"),
])
def test_input_can_reference_existing_cae_objects(kind, value, cls, name):
    v = sink([("i", "input.value", {"type": kind, "value": value})], [], "i", "value")
    assert type(v).__name__ == cls and v.name == name and v.existing


def test_steps_and_frames():
    base = [("odb", "odb.open", {"path": MOCK}), ("t", "odb.timeline", {"first": -3})]
    e = [("odb", "odb", "t", "odb")]
    assert len(sink(base, e, "t", "frames")) == 3
    assert len(sink(base, e, "t", "steps")) >= 1


def test_open_odb_folder_and_wildcards(tmp_path):
    from nodecode_fea.nodes import _odb_files
    (tmp_path / "sub").mkdir()
    for name in ("a.odb", "b.odb", "notes.txt", "sub/c.odb"):
        (tmp_path / name).write_text("x")
    assert [p[-5:] for p in _odb_files(str(tmp_path))] == ["a.odb", "b.odb"]
    assert len(_odb_files(str(tmp_path / "*.odb"))) == 2
    assert len(_odb_files(str(tmp_path / "**" / "*.odb"))) == 3
    assert _odb_files(str(tmp_path / "a.odb")) is None  # a plain file opens as before
    with pytest.raises(ValueError, match="No files match"):
        _odb_files(str(tmp_path / "zz*.odb"))


def test_value_shows_fields_and_texts(tmp_path):
    nodes = [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
             ("q", "field.quantity", {"quantity": "Mises"}), ("v", "view.value", {"limit": 5}),
             ("t", "input.value", {"type": "Text", "value": "hello"}), ("vt", "view.value", {})]
    res, inputs = ev(nodes, [("odb", "odb", "s", "odb"), ("s", "field", "q", "field"), ("q", "field", "v", "value"),
                             ("t", "value", "vt", "value")])
    assert res["v"]["display"]["kind"] == "table" and len(res["v"]["display"]["rows"]) == 5
    assert res["vt"]["status"] == "ok"


def test_palette_is_small():
    visible = [d for d in REGISTRY.values() if not d.hidden]
    assert len(visible) <= 46  # Datum, boolean assembly, analysis Job, Properties and Amplitude are genuinely different operations
    for d in REGISTRY.values():
        if d.hidden:
            assert d.replaced_by in REGISTRY and not REGISTRY[d.replaced_by].hidden, d.type
