import numpy as np
import pytest
from fastapi.testclient import TestClient

from nodecode_fea.app import app
from nodecode_fea.nodes import parse_labels, parse_points
from nodecode_fea.sources import open_source

from .test_engine import E, I, MOCK, P, L, run


def deflection(x, t=1.0):
    return -P * t * x**2 * (3 * L - x) / (6 * E * I)


def test_parse_labels():
    assert parse_labels("BEAM-1.533, 540 10:12") == [("BEAM-1", 533), ("", 540), ("", 10), ("", 11), ("", 12)]
    with pytest.raises(ValueError):
        parse_labels("abc")
    with pytest.raises(ValueError):
        parse_labels("5:2")


def test_parse_points():
    assert parse_points("0,0; 1 2 3").tolist() == [[0, 0, 0], [1, 2, 3]]
    with pytest.raises(ValueError):
        parse_points("1,2,3")


def test_probe_one_curve_per_label():
    tip = open_source(MOCK).tip_node
    res = run(
        [("odb", "odb.open", {"path": MOCK}),
         ("p", "field.probe", {"variable": "U", "quantity": "U2", "labels": f"BEAM-1.{tip}, 1"}),
         ("c", "view.chart", {})],
        [("odb", "odb", "p", "odb"), ("p", "series", "c", "series")],
    )
    assert res["p"]["status"] == "ok", res["p"]
    curves = res["c"]["display"]["series"]
    assert len(curves) == 2
    assert curves[0]["y"][-1] == pytest.approx(deflection(L))
    assert curves[1]["y"] == pytest.approx([0.0] * 11)  # node 1 is on the fixed end


def test_probe_element_field_and_bad_label():
    res = run(
        [("odb", "odb.open", {"path": MOCK}),
         ("p", "field.probe", {"variable": "S", "quantity": "Mises", "labels": "1"}),
         ("bad", "field.probe", {"variable": "S", "quantity": "Mises", "labels": "99999"}),
         ("pk", "series.reduce", {"op": "Final"})],
        [("odb", "odb", "p", "odb"), ("odb", "odb", "bad", "odb"), ("p", "series", "pk", "series")],
    )
    assert res["pk"]["status"] == "ok"
    assert res["pk"]["display"]["value"] > 0
    assert res["bad"]["status"] == "error" and "99999" in res["bad"]["message"]


@pytest.mark.parametrize("method", ["Interpolate", "Nearest node"])
def test_path_matches_beam_deflection(method):
    # 41 samples over 0..100 land exactly on the mesh's node columns (spacing 2.5).
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("u", "odb.field", {"variable": "U"}),
         ("c", "field.component", {"component": "U2"}),
         ("path", "field.path", {"points": "0, 0, 0; 100, 0, 0", "samples": 41, "method": method})],
        [("odb", "odb", "u", "odb"), ("u", "field", "c", "field"), ("c", "field", "path", "field")],
    )
    assert res["path"]["status"] == "ok", res["path"]
    s = res["path"]["display"]["series"][0]
    x = np.array(s["x"])
    assert x[-1] == pytest.approx(100)
    assert s["y"] == pytest.approx(deflection(x).tolist(), abs=1e-9)


def test_path_interpolates_between_nodes():
    # Halfway between two node columns trilinear interpolation gives the mean of the two.
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("u", "odb.field", {"variable": "U"}),
         ("c", "field.component", {"component": "U2"}),
         ("path", "field.path", {"points": "1.25, 0, 0; 3.75, 0, 0", "samples": 2})],
        [("odb", "odb", "u", "odb"), ("u", "field", "c", "field"), ("c", "field", "path", "field")],
    )
    y = res["path"]["display"]["series"][0]["y"]
    assert y[0] == pytest.approx((deflection(0) + deflection(2.5)) / 2)
    assert y[1] == pytest.approx((deflection(2.5) + deflection(5)) / 2)


def test_path_outside_model_errors():
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("u", "odb.field", {"variable": "U"}),
         ("c", "field.component", {"component": "U2"}),
         ("path", "field.path", {"points": "0, 500, 0; 100, 500, 0"})],
        [("odb", "odb", "u", "odb"), ("u", "field", "c", "field"), ("c", "field", "path", "field")],
    )
    assert res["path"]["status"] == "error"


def test_contour_marks_reduce_location():
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
         ("m", "field.invariant", {"invariant": "Mises"}), ("max", "field.reduce", {"op": "Max"}),
         ("mean", "field.reduce", {"op": "Mean"}), ("c", "view.contour", {})],
        [("odb", "odb", "s", "odb"), ("s", "field", "m", "field"), ("m", "field", "max", "field"),
         ("m", "field", "mean", "field"), ("m", "field", "c", "field"),
         ("max", "value", "c", "markers"), ("mean", "value", "c", "markers")],
    )
    d = res["c"]["display"]
    assert len(d["markers"]) == 1
    x, y, z = d["markers"][0]["position"]
    assert x == pytest.approx(L / 40 / 2) and abs(y) == pytest.approx(10 / 2 - 10 / 4 / 2)
    assert d["notes"] and d["notes"][0].startswith("Mean")


def test_browse_lists_odbs(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "job.odb").write_text("x")
    (tmp_path / "notes.txt").write_text("x")
    client = TestClient(app)
    r = client.get("/api/browse", params={"path": str(tmp_path)}).json()
    assert r["dirs"] == ["sub"]
    assert [f["name"] for f in r["files"]] == ["job.odb"]
    assert client.get("/api/browse", params={"path": str(tmp_path / "nope")}).status_code == 400


def test_browse_lists_the_files_a_setting_accepts(tmp_path):
    for name in ("job.odb", "bracket.STEP", "plate.igs", "notes.txt"):
        (tmp_path / name).write_text("x")
    client = TestClient(app)
    r = client.get("/api/browse", params={"path": str(tmp_path), "extensions": ".step,.stp,.igs"}).json()
    assert [f["name"] for f in r["files"]] == ["bracket.STEP", "plate.igs"]
    from nodecode_fea.nodes import REGISTRY
    part = next(p for p in REGISTRY["model.part"].params if p.name == "file")
    assert ".step" in part.options and ".odb" not in part.options


def test_contour_results_take_fields_and_sets():
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
         ("m", "field.invariant", {"invariant": "Mises"}), ("hot", "field.where", {"op": ">", "value": 440}),
         ("many", "field.where", {"op": ">", "value": 200}), ("tip", "odb.set", {"name": "TIP"}),
         ("c", "view.contour", {})],
        [("odb", "odb", "s", "odb"), ("odb", "odb", "tip", "odb"), ("s", "field", "m", "field"),
         ("m", "field", "hot", "field"), ("m", "field", "many", "field"), ("m", "field", "c", "field"),
         ("hot", "field", "c", "markers"), ("many", "field", "c", "markers"), ("tip", "set", "c", "markers")],
    )
    d = res["c"]["display"]
    assert res["c"]["status"] == "ok", res["c"]
    hot = res["hot"]["display"]["total"]
    many = res["many"]["display"]["total"]
    assert len(d["markers"]) == hot + many + 25
    assert all(m["text"] for m in d["markers"][:hot])  # few: labelled with their values
    assert not any(m["text"] for m in d["markers"][hot:hot + many])  # many: dots only
    assert any("TIP: 25 nodes marked" in n for n in d["notes"])
