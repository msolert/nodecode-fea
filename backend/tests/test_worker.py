"""Run the real odb_worker.py through AbaqusSource, against a fake odbAccess module.

This checks the worker protocol, array encoding, set flattening and the
integration-point averaging without needing an Abaqus licence.
"""


import numpy as np
import pytest

from nodecode_fea.sources import abaqus as abaqus_mod
from nodecode_fea.sources.abaqus import AbaqusSource, Worker
from nodecode_fea.sources.mock import MockSource

from .fake_paths import LAUNCHER as FAKE
URL = "mock://cantilever?nx=8&ny=2&nz=2&frames=4"


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    odb = tmp_path_factory.mktemp("odb") / "beam.odb"
    odb.write_text(URL)
    mp = pytest.MonkeyPatch()
    mp.setenv("ABAQUS_CMD", str(FAKE))
    Worker._instance = None
    yield AbaqusSource(str(odb)), MockSource(URL)
    Worker.get().proc.kill()
    Worker._instance = None
    mp.undo()


def test_describe_matches_mock(pair):
    real, mock = pair
    a, b = real.describe(), mock.describe()
    assert [s["name"] for s in a["steps"]] == [s["name"] for s in b["steps"]]
    assert a["steps"][0]["frames"] == b["steps"][0]["frames"]
    assert {f["name"]: f["positions"] for f in a["steps"][0]["fields"]} == {"U": ["NODAL"], "RF": ["NODAL"],
                                                                             "S": ["ELEMENT"], "E": ["ELEMENT"],
                                                                             "PEEQ": ["ELEMENT"]}
    # The worker passes on which invariants Abaqus defines for each variable.
    assert {f["name"]: f["invariants"] for f in a["steps"][0]["fields"]} == \
        {f["name"]: f["invariants"] for f in b["steps"][0]["fields"]}
    assert sorted(s["name"] for s in a["sets"]) == sorted(s["name"] for s in b["sets"])
    assert a["instances"] == b["instances"]


@pytest.mark.parametrize("var", ["U", "S"])
def test_field_matches_mock(pair, var):
    real, mock = pair
    a, b = real.field("Load", -1, var), mock.field("Load", -1, var)
    assert a["position"] == b["position"]
    order_a, order_b = np.argsort(a["labels"]), np.argsort(b["labels"])
    assert np.array_equal(a["labels"][order_a], b["labels"][order_b])
    assert np.allclose(a["values"][order_a], b["values"][order_b])
    assert set(a["instances"]) == {"BEAM-1"}


def test_integration_points_are_preserved_when_requested(pair):
    real, mock = pair
    averaged = real.field("Load", -1, "S")
    raw = real.field("Load", -1, "S", position="Integration point")
    assert len(raw["labels"]) == 2 * len(averaged["labels"])
    assert set(raw["integrationPoints"]) == {1, 2}
    assert set(raw["sectionPoints"]) == {1}
    # The fake uses 0.9/1.1 at the two points, whose mean is the legacy automatic value.
    assert np.allclose(raw["values"].reshape(len(averaged["values"]), 2, -1).mean(axis=1), averaged["values"])


def test_element_nodal_rows_are_told_apart_by_node(pair):
    real, _ = pair
    raw = real.field("Load", -1, "S", position="Element nodal")
    keys = set(zip(raw["labels"].tolist(), raw["integrationPoints"].tolist()))
    assert len(keys) == len(raw["labels"])  # every row unique, so Formula/Combine frames line them up
    assert raw["integrationPoints"][0] == raw["labels"][0] * 10 + 1


def test_section_point_filter_reports_missing_point(pair):
    real, _ = pair
    one = real.field("Load", -1, "S", position="Integration point", section_point=1)
    assert len(one["labels"]) and set(one["sectionPoints"]) == {1}
    with pytest.raises(abaqus_mod.SourceError, match="section point 2"):
        real.field("Load", -1, "S", position="Integration point", section_point=2)


def test_complex_field_components_and_phase_angle(pair):
    real, _ = pair
    re = real.field("Load", -1, "U", complex_part="Real")["values"]
    im = real.field("Load", -1, "U", complex_part="Imaginary")["values"]
    mag = real.field("Load", -1, "U", complex_part="Magnitude")["values"]
    phase = real.field("Load", -1, "U", complex_part="Phase")["values"]
    at_90 = real.field("Load", -1, "U", complex_part="At phase angle", phase_angle=90)["values"]
    assert np.allclose(im, re * 0.5)
    assert np.allclose(mag, np.hypot(re, im))
    assert np.allclose(phase[re != 0], np.degrees(np.arctan2(im, re))[re != 0])
    assert np.allclose(at_90, -im)


@pytest.mark.parametrize("name", ["FIXED", "ROOT_ELEMENTS", "BEAM-1.ALL_NODES"])
def test_sets_match_mock(pair, name):
    real, mock = pair
    a, b = real.set(name), mock.set(name)
    assert a["kind"] == b["kind"]
    assert sorted(a["labels"].tolist()) == sorted(b["labels"].tolist())
    assert set(a["instances"]) == {"BEAM-1"}


def test_history_and_mesh(pair):
    real, mock = pair
    a, b = real.history("Load", "Node set FIXED", "RF2"), mock.history("Load", "Node set FIXED", "RF2")
    assert np.allclose(a["y"], b["y"])
    ma, mb = real.mesh()["instances"][0], mock.mesh()["instances"][0]
    assert np.allclose(ma["coords"], mb["coords"])
    assert ma["connectivity"] == [list(c) for c in mb["connectivity"]]


def test_worker_errors_are_reported(pair):
    real, _ = pair
    with pytest.raises(abaqus_mod.SourceError, match="Field 'SDV1' not in frame"):
        real.field("Load", -1, "SDV1")
    with pytest.raises(abaqus_mod.SourceError, match="Step 'Nope' not found"):
        real.field("Nope", 0, "U")
