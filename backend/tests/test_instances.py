"""Models with several part instances that reuse the same labels, and assembly-level sets spanning them.

Each test runs twice: on the mock directly, and on a real-path ODB read by the Abaqus
worker (through the fake odbAccess), which exercises the worker's set handling.
"""

import pytest

from nodecode_fea.sources.abaqus import Worker

from .test_engine import run
from .test_worker import FAKE

URL = "mock://cantilever?instances=2&rp=1&nx=8&ny=2&nz=2&frames=4"
TIP_NODES = 3 * 3  # (ny+1) x (nz+1) nodes on each tip face


@pytest.fixture(scope="module", params=["mock", "worker"])
def odb(request, tmp_path_factory):
    if request.param == "mock":
        yield URL
        return
    path = tmp_path_factory.mktemp("inst") / "two_beams.odb"
    path.write_text(URL)
    mp = pytest.MonkeyPatch()
    mp.setenv("ABAQUS_CMD", str(FAKE))
    Worker._instance = None
    yield str(path)
    Worker.get().proc.kill()
    Worker._instance = None
    mp.undo()


def test_field_vs_time_each_on_assembly_set(odb):
    res = run(
        [("odb", "odb.open", {"path": odb}), ("tip", "odb.set", {"name": "TIP"}),
         ("each", "field.over_frames", {"variable": "U", "quantity": "U2", "op": "Each (one curve per item)"})],
        [("odb", "odb", "tip", "odb"), ("odb", "odb", "each", "odb"), ("tip", "set", "each", "set")],
    )
    assert res["each"]["status"] == "ok", res["each"]
    curves = res["each"]["display"]["series"]
    assert len(curves) == 2 * TIP_NODES
    beam1 = [c for c in curves if "BEAM-1." in c["name"]]
    beam2 = [c for c in curves if "BEAM-2." in c["name"]]
    assert len(beam1) == len(beam2) == TIP_NODES
    # BEAM-2 carries twice the load, so its tip deflects twice as much.
    assert beam2[0]["y"][-1] == pytest.approx(2 * beam1[0]["y"][-1])


def test_max_over_assembly_element_set(odb):
    res = run(
        [("odb", "odb.open", {"path": odb}), ("root", "odb.set", {"name": "ROOT_ELEMENTS"}),
         ("s", "odb.field", {"variable": "S"}), ("m", "field.invariant", {}),
         ("f", "field.filter", {}), ("max", "field.reduce", {"op": "Max"}),
         ("t", "field.over_frames", {"variable": "S", "quantity": "Mises", "op": "Max"})],
        [("odb", "odb", "root", "odb"), ("odb", "odb", "s", "odb"), ("s", "field", "m", "field"),
         ("m", "field", "f", "field"), ("root", "set", "f", "set"), ("f", "field", "max", "field"),
         ("odb", "odb", "t", "odb"), ("root", "set", "t", "set")],
    )
    assert res["f"]["display"]["total"] == 2 * 2 * 2  # root column (2 x 2 elements) in both instances
    assert res["max"]["display"]["where"].startswith("element BEAM-2.")
    assert res["t"]["display"]["series"][0]["y"][-1] == pytest.approx(res["max"]["display"]["value"])


def test_probe_set_with_label_dropdown(odb):
    res = run(
        [("odb", "odb.open", {"path": odb}), ("tip", "odb.set", {"name": "TIP"}),
         ("all", "field.probe", {"variable": "U", "quantity": "U2", "labels": ""}),
         ("two", "field.probe", {"variable": "U", "quantity": "U2", "labels": "BEAM-2.9, BEAM-1.9"}),
         ("amb", "field.probe", {"variable": "U", "quantity": "U2", "labels": "9"})],
        [("odb", "odb", "tip", "odb"), ("tip", "set", "all", "set"), ("tip", "set", "two", "set"),
         ("odb", "odb", "all", "odb"), ("odb", "odb", "two", "odb"), ("odb", "odb", "amb", "odb")],
    )
    options = [o["value"] for o in res["all"]["hints"]["labels"]]
    assert len(options) == 2 * TIP_NODES and "BEAM-2.9" in options
    assert res["all"]["summary"].startswith(f"{2 * TIP_NODES} curves")
    assert res["two"]["summary"].startswith("2 curves")
    assert res["amb"]["status"] == "error" and "INSTANCE.9" in res["amb"]["message"]


def test_sets_of_the_other_kind_are_converted(odb):
    res = run(
        [("odb", "odb.open", {"path": odb}), ("u", "odb.field", {"variable": "U"}),
         ("root", "odb.set", {"name": "ROOT_ELEMENTS"}), ("f", "field.filter", {})],
        [("odb", "odb", "u", "odb"), ("odb", "odb", "root", "odb"), ("u", "field", "f", "field"),
         ("root", "set", "f", "set")],
    )
    # Nodes of the 2 x 2 root elements (one element column long): 2 x 3 x 3 per instance.
    assert res["f"]["display"]["total"] == 2 * 2 * 3 * 3


def test_assembly_level_reference_point(odb):
    """Sets holding assembly-level nodes (instance 'rootAssembly' / none) next to part nodes with the same label."""
    res = run(
        [("odb", "odb.open", {"path": odb}), ("rp", "odb.set", {"name": "RP"}),
         ("mixed", "odb.set", {"name": "TIP_AND_RP"}), ("u", "odb.field", {"variable": "U"}),
         ("only_rp", "field.filter", {}), ("both", "field.filter", {}),
         ("p_rp", "field.probe", {"variable": "U", "quantity": "U2", "labels": ""}),
         ("p_mixed", "field.probe", {"variable": "U", "quantity": "U2", "labels": ""}),
         ("typed", "field.probe", {"variable": "U", "quantity": "U2", "labels": "ASSEMBLY.1, BEAM-2.1"}),
         ("amb", "field.probe", {"variable": "U", "quantity": "U2", "labels": "1"}),
         ("each", "field.over_frames", {"variable": "U", "quantity": "U2", "op": "Each (one curve per item)"})],
        [("odb", "odb", "rp", "odb"), ("odb", "odb", "mixed", "odb"), ("odb", "odb", "u", "odb"),
         ("u", "field", "only_rp", "field"), ("rp", "set", "only_rp", "set"),
         ("u", "field", "both", "field"), ("mixed", "set", "both", "set"),
         ("odb", "odb", "p_rp", "odb"), ("rp", "set", "p_rp", "set"),
         ("odb", "odb", "p_mixed", "odb"), ("mixed", "set", "p_mixed", "set"),
         ("odb", "odb", "typed", "odb"), ("odb", "odb", "amb", "odb"),
         ("odb", "odb", "each", "odb"), ("rp", "set", "each", "set")],
    )
    for nid in ("rp", "mixed", "only_rp", "both", "p_rp", "p_mixed", "typed", "each"):
        assert res[nid]["status"] == "ok", (nid, res[nid])
    rows = res["only_rp"]["display"]["rows"]
    assert len(rows) == 1 and rows[0][0] == "" and rows[0][1] == 1  # the RP, not node 1 of a part
    assert res["both"]["display"]["total"] == TIP_NODES + 1
    curve = res["p_rp"]["display"]["series"][0]
    assert curve["name"] == "U2 @ ASSEMBLY.1" and curve["y"][-1] < 0  # it follows BEAM-1's tip down
    assert res["p_mixed"]["summary"].startswith(f"{TIP_NODES + 1} curves")
    assert "ASSEMBLY.1" in [o["value"] for o in res["p_mixed"]["hints"]["labels"]]
    assert [c["name"] for c in res["typed"]["display"]["series"]] == ["U2 @ ASSEMBLY.1", "U2 @ BEAM-2.1"]
    assert res["amb"]["status"] == "error" and "ASSEMBLY" in res["amb"]["message"]
    assert res["each"]["display"]["series"][0]["name"] == "U2 @ ASSEMBLY.1"
