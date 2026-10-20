"""Sets and Surfaces made in CAE come into a Region as Find-at points ("From a CAE set"). Checked against real
Abaqus 2025 too (each point finds the same entity again with findAt); here with small stand-ins for CAE's objects."""

import contextlib
import io
import types

from nodecode_fea import cae_sets


class E:
    def __init__(self, point, instance=None):
        self.pointOn = (point,)
        if instance is not None:
            self.instanceName = instance


def ns(**kw):
    return types.SimpleNamespace(**kw)


def fake_mdb():
    top, side = E((0.666667, 1, 2)), E((0, 0.333333, 2))
    part = ns(sets={"TwoFaces": ns(faces=[top, side, top], edges=[], vertices=[], cells=[]),
                    "AnEdge": ns(faces=[], edges=[E((1.5, 0, 0))], vertices=[], cells=[]),
                    "NodesOnly": ns(faces=[], edges=[], vertices=[], cells=[], nodes=[1, 2]),
                    "_internal": ns(faces=[top], edges=[], vertices=[], cells=[])},
              surfaces={"TopSurf": ns(faces=[top], edges=[])})
    assembly = ns(sets={"Tops": ns(faces=[E((0.5, 1, 2), "Box-1"), E((10.5, 1, 2), "Box-2")], edges=[],
                                   vertices=[], cells=[])},
                  surfaces={})
    return ns(models={"Model-1": ns(parts={"Box": part}, rootAssembly=assembly)})


def test_sets_become_find_at_points_per_owner_and_kind():
    items = cae_sets.defined_sets(fake_mdb())
    assert [(i["part"] or i["instance"], i["name"], i["form"], i["kind"], i["count"], i["value"]) for i in items] == [
        ("Box", "AnEdge", "Set", "edge", 1, "@1.5,0,0"),
        ("Box", "TwoFaces", "Set", "face", 2, "@0.666667,1,2;0,0.333333,2"),  # the repeated face only once
        ("Box", "TopSurf", "Surface", "face", 1, "@0.666667,1,2"),
        ("Box-1", "Tops", "Set", "face", 1, "@0.5,1,2"),  # an assembly set: one Region per instance
        ("Box-2", "Tops", "Set", "face", 1, "@10.5,1,2"),
    ]


def test_the_cae_script_prints_one_machine_line(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "abaqus", ns(mdb=fake_mdb()))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(cae_sets.script(), "list_sets.py", "exec"), {"__name__": "__nocode__"})
    assert cae_sets.parse(out.getvalue()) == cae_sets.defined_sets(fake_mdb())
    assert cae_sets.parse("nothing here") is None
