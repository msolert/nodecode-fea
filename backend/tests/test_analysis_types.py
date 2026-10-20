"""2D planar and axisymmetric parts, rigid surfaces, and the analyses and modelling tools added for them."""

import pytest

from .test_cae_export import calls, run_in_fake_cae
from .test_model import evaluate, script


def flat(space="2D planar", part=None, element=None, section=None, sketch=None):
    nodes = [("s", "model.sketch", dict({"width": 100, "height": 20}, **(sketch or {}))),
             ("p", "model.part", dict({"space": space}, **(part or {}))),
             ("sec", "model.section", section or {}), ("mesh", "model.mesh", {"element": element or "Automatic"}),
             ("i", "model.instance", {})]
    edges = [("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"), ("sec", "part", "mesh", "target"),
             ("mesh", "part", "i", "part")]
    return nodes, edges


@pytest.mark.parametrize("space, dimensionality, element", [("2D planar", "TWO_D_PLANAR", "CPS4R"),
                                                            ("Axisymmetric", "AXISYMMETRIC", "CAX4R")])
def test_a_flat_part_is_built_in_its_space_with_a_solid_section(space, dimensionality, element, tmp_path):
    out, bad = script(*flat(space, section={"plane_thickness": 2}))
    assert not bad, bad
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, ".Part")[0]["kwargs"]["dimensionality"] == dimensionality
    assert calls(log, "BaseShell")
    section = calls(log, "HomogeneousSolidSection")[0]["kwargs"]
    assert section["thickness"] == (2.0 if space == "2D planar" else None)  # plane stress thickness
    assert f"elemCode={element}" in out["script"]  # Automatic picks the family of the space
    if space == "Axisymmetric":  # the axis it turns about
        assert "s.ConstructionLine(point1=(0.0, -100.0), point2=(0.0, 100.0))" in out["script"]


@pytest.mark.parametrize("change, message", [
    ({"part": {"method": "Extrude"}}, "flat profile itself"),
    ({"part": {"body": "Solid"}}, "Body Shell"),
    ({"element": "C3D8R"}, "CPS… or CPE…"),
    ({"element": "CPS4R", "space": "Axisymmetric"}, "CAX…"),
    ({"space": "Axisymmetric", "sketch": {"x": -10}}, "x ≥ 0"),
])
def test_flat_parts_refuse_what_does_not_fit_them(change, message):
    nodes, edges = flat(change.get("space", "2D planar"), change.get("part"), change.get("element"),
                        sketch=change.get("sketch"))
    _, res, _ = evaluate(nodes, edges)
    errors = " ".join(r["message"] for r in res.values() if r["status"] != "ok")
    assert message in errors, errors


def test_a_flat_instance_stays_in_its_plane():
    nodes, edges = flat()
    nodes[-1] = ("i", "model.instance", {"z": 5})
    _, res, _ = evaluate(nodes, edges)
    assert "stays in the XY plane" in res["i"]["message"]
    nodes, edges = flat("Axisymmetric")
    nodes[-1] = ("i", "model.instance", {"x": 5})
    _, res, _ = evaluate(nodes, edges)
    assert "only moves along y" in res["i"]["message"]


def test_pressure_on_a_2d_part_goes_on_its_edges():
    """(Solved in real Abaqus: test_flat_bars_stretch_as_hookes_law_says.)"""
    nodes, edges = flat()
    nodes += [("st", "model.step", {}), ("pts", "input.value", {"type": "Text", "value": "100, 10, 0"}),
              ("e", "model.region", {"method": "Find at", "kind": "Edge"}),
              ("pr", "model.condition", {"kind": "Pressure", "magnitude": -10})]
    edges += [("i", "instance", "e", "source"), ("pts", "value", "e", "points"), ("st", "step", "pr", "step"),
              ("e", "region", "pr", "region")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "'side1Edges'" in out["script"] and "m.Pressure(" in out["script"]
    nodes[-2] = ("e", "model.region", {"method": "Find at", "kind": "Face"})
    _, res, _ = evaluate(nodes, edges)
    assert "Edge Region on a 2D part" in res["pr"]["message"]


def rigid(part, profile="-5, 10; 25, 10"):
    return ([("s", "model.sketch", {"shape": "Profile", "points": profile}), ("p", "model.part", part),
             ("rp", "model.feature", {"operation": "Reference point", "point": "10, 15, 0"})],
            [("s", "sketch", "p", "profile"), ("p", "part", "rp", "part")])


@pytest.mark.parametrize("part, command", [
    ({"space": "2D planar", "type": "Analytical rigid", "body": "Wire"}, "AnalyticRigidSurf2DPlanar"),
    ({"space": "Axisymmetric", "type": "Analytical rigid", "body": "Wire"}, "AnalyticRigidSurf2DPlanar"),
    ({"type": "Analytical rigid", "method": "Extrude", "depth": 30}, "AnalyticRigidSurfExtrude"),
    ({"type": "Analytical rigid", "method": "Revolve"}, "AnalyticRigidSurfRevolve"),
    ({"type": "Discrete rigid", "method": "Extrude", "depth": 30}, "BaseShellExtrude"),
])
def test_rigid_surfaces_are_built_as_rigid_parts(part, command, tmp_path):
    profile = "5, 0; 5, 20" if part.get("method") == "Revolve" or part.get("space") == "Axisymmetric" else \
        "-5, 10; 25, 10"
    out, bad = script(*rigid(part, profile))
    assert not bad, bad
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, command)
    kind = "ANALYTIC_RIGID_SURFACE" if part["type"] == "Analytical rigid" else "DISCRETE_RIGID_SURFACE"
    assert calls(log, ".Part")[0]["kwargs"]["type"] == kind


@pytest.mark.parametrize("extra, message", [
    ({"type": "Analytical rigid", "body": "Shell", "space": "2D planar"}, "is a Wire"),
    ({"type": "Analytical rigid", "method": "Revolve", "angle": 90}, "Angle 360"),
    ({"type": "Discrete rigid", "body": "Solid", "method": "Extrude"}, "a Shell in 3D"),
])
def test_rigid_parts_refuse_bodies_abaqus_cannot_make(extra, message):
    _, res, _ = evaluate(*rigid(dict({"method": "Planar"}, **extra), "5, 0; 5, 20"))
    assert message in res["p"]["message"], res["p"]


def test_rigid_parts_take_no_section_and_analytical_ones_no_mesh():
    nodes, edges = rigid({"space": "2D planar", "type": "Analytical rigid", "body": "Wire"})
    _, res, _ = evaluate(nodes + [("sec", "model.section", {}), ("mesh", "model.mesh", {})],
                         edges + [("rp", "part", "sec", "target"), ("rp", "part", "mesh", "target")])
    assert "takes no material or section" in res["sec"]["message"]
    assert "has no mesh" in res["mesh"]["message"]
    nodes, edges = rigid({"type": "Discrete rigid", "method": "Extrude", "depth": 30})
    out, bad = script(nodes + [("mesh", "model.mesh", {})], edges + [("rp", "part", "mesh", "target")])
    assert not bad, bad
    assert "elemCode=R3D4" in out["script"]


def test_a_rigid_part_without_reference_point_and_mixed_spaces_are_flagged():
    from .test_checks import lint
    nodes = [("m", "model.model", {"name": "M"}), ("s", "model.sketch", {"shape": "Profile", "points": "0, 0; 10, 0"}),
             ("p", "model.part", {"space": "2D planar", "type": "Analytical rigid", "body": "Wire"}),
             ("i", "model.instance", {}), ("b", "model.sketch", {"name": "B"}),
             ("q", "model.part", {"name": "Q"}), ("sec", "model.section", {}), ("mesh", "model.mesh", {}),
             ("j", "model.instance", {}), ("st", "model.step", {})]
    edges = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "i", "part"),
             ("m", "model", "b", "model"), ("b", "sketch", "q", "profile"), ("q", "part", "sec", "target"),
             ("sec", "part", "mesh", "target"), ("mesh", "part", "j", "part"), ("m", "model", "st", "after")]
    warnings = lint(nodes, edges)
    assert any("has no reference point" in w for w in warnings["i"]), warnings
    assert not any("Material & Section" in w for w in warnings["i"])  # rigid: no section needed
    assert any("mixes 2D planar and 3D parts" in w for w in warnings["i"] + warnings["j"]), warnings


def box(step=None, conditions=(), section=None, element="C3D8R"):
    """A meshed box placed in the assembly, a step, and conditions [(id, params, region kind, point)]."""
    nodes = [("m", "model.model", {"name": "M"}), ("s", "model.sketch", {}),
             ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("sec", "model.section", section or {}), ("mesh", "model.mesh", {"element": element}),
             ("i", "model.instance", {}), ("st", "model.step", step or {})]
    edges = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"),
             ("sec", "part", "mesh", "target"), ("mesh", "part", "i", "part"), ("m", "model", "st", "after")]
    for cid, params, kind, at in conditions:
        nodes += [(cid + "_at", "input.value", {"type": "Text", "value": at}),
                  (cid + "_r", "model.region", {"method": "Find at", "kind": kind}), (cid, "model.condition", params)]
        edges += [("i", "instance", cid + "_r", "source"), (cid + "_at", "value", cid + "_r", "points"),
                  (cid + "_r", "region", cid, "region"), ("st", "step", cid, "step")]
    return nodes, edges


@pytest.mark.parametrize("step, call", [
    ({"kind": "Buckling", "modes": 3}, "m.BuckleStep(name='Step-1', previous='Initial', numEigen=3"),
    ({"kind": "Static Riks", "max_load_factor": 2}, "m.StaticRiksStep(name='Step-1', previous='Initial', nlgeom=ON"),
    ({"kind": "Heat transfer", "response": "Transient", "max_temperature_change": 5},
     "m.HeatTransferStep(name='Step-1', previous='Initial', response=TRANSIENT"),
    ({"kind": "Coupled temperature-displacement"},
     "m.CoupledTempDisplacementStep(name='Step-1', previous='Initial', response=STEADY_STATE"),
])
def test_new_procedures_make_their_steps(step, call, tmp_path):
    out, bad = script(*box(step, element="DC3D8" if step["kind"] == "Heat transfer" else
                           "C3D8T" if step["kind"].startswith("Coupled") else "C3D8R"))
    assert not bad, bad
    assert call in out["script"], out["script"]
    if step["kind"] == "Static Riks":
        assert "maxLPF=2.0" in out["script"]
    if step["kind"] == "Heat transfer":
        assert "deltmx=5.0" in out["script"]
    log, stdout = run_in_fake_cae(out["script"], tmp_path)
    assert "failed" not in stdout, stdout


def test_steps_refuse_procedures_that_cannot_follow_each_other():
    nodes, edges = box({"kind": "Heat transfer"})
    nodes += [("st2", "model.step", {"name": "Load"})]
    edges += [("st", "step", "st2", "after")]
    _, res, _ = evaluate(nodes, edges)
    assert "only heat transfer steps" in res["st2"]["message"]
    nodes, edges = box({"kind": "Dynamic implicit"})
    nodes += [("st2", "model.step", {"name": "Buckle", "kind": "Buckling"})]
    edges += [("st", "step", "st2", "after")]
    _, res, _ = evaluate(nodes, edges)
    assert "perturbation step" in res["st2"]["message"]


def test_thermal_conditions_need_temperatures_and_make_their_cae_objects():
    conditions = [("hot", {"kind": "Fixed temperature", "temperature": 100}, "Face", "0, 25, 5"),
                  ("q", {"kind": "Heat flux", "flux": 2}, "Face", "100, 25, 5"),
                  ("air", {"kind": "Convection", "film": 1e-4, "sink": 25}, "Face", "50, 50, 5")]
    out, bad = script(*box({"kind": "Heat transfer"}, conditions, {"conductivity": 0.05}, "DC3D8"))
    assert not bad, bad
    for call in ("m.TemperatureBC(name='Condition-1'", "magnitude=100.0", "m.SurfaceHeatFlux(", "magnitude=2.0",
                 "m.FilmCondition(", "filmCoeff=0.0001, ", "sinkTemperature=25.0", "definition=EMBEDDED_COEFF"):
        assert call in out["script"], call
    _, res, _ = evaluate(*box({}, conditions[:1]))
    assert "needs a Heat transfer or Coupled" in res["hot"]["message"]
    _, res, _ = evaluate(*box({"kind": "Heat transfer"}, [("f", {"kind": "Fixed"}, "Face", "0, 25, 5")],
                              element="DC3D8"))
    assert "temperatures only" in res["f"]["message"]


def test_thermal_models_are_checked_for_conductivity_and_elements():
    from .test_checks import lint
    warnings = lint(*box({"kind": "Heat transfer", "response": "Transient"},
                         [("hot", {"kind": "Fixed temperature"}, "Face", "0, 25, 5")]))
    assert any("no conductivity" in w for w in warnings["sec"]), warnings
    assert any("Specific heat and Density" in w for w in warnings["sec"]), warnings
    assert any("heat transfer elements" in w for w in warnings["mesh"]), warnings


@pytest.mark.parametrize("fix, call", [("X symmetry", "XsymmBC"), ("Z antisymmetry", "ZasymmBC"),
                                       ("Pinned (U1, U2, U3)", "PinnedBC"), ("All (encastre)", "EncastreBC")])
def test_fixed_offers_symmetry_and_pinned_supports(fix, call, tmp_path):
    out, bad = script(*box({}, [("f", {"kind": "Fixed", "fix": fix}, "Face", "0, 25, 5")]))
    assert not bad, bad
    assert f"m.{call}(name='Condition-1'" in out["script"]


def test_a_coordinate_system_datum_turns_loads_and_supports():
    nodes, edges = box({}, [("f", {"kind": "Fixed", "fix": "X symmetry"}, "Face", "0, 25, 5"),
                            ("d", {"kind": "Displacement / rotation", "u1": "0.1"}, "Face", "100, 25, 5")])
    nodes += [("cpts", "input.value", {"type": "Text", "value": "0, 0, 0; 1, 0, 0; 0, 1, 0"}),
              ("cs", "model.datum", {"name": "Radial", "kind": "Coordinate system", "csys": "Cylindrical"})]
    edges += [("m", "model", "cs", "part"), ("cpts", "value", "cs", "points"), ("cs", "datum", "f", "csys"),
              ("cs", "datum", "d", "csys")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert "DatumCsysByThreePoints(coordSysType=CYLINDRICAL" in out["script"]
    assert "_csys = m.rootAssembly.datums[m.rootAssembly.features['Radial'].id]" in out["script"]
    assert out["script"].count("localCsys=_csys") == 2
    # a part's coordinate system is refused: loads are in the assembly
    edges = [e for e in edges if e[0] != "m" or e[2] != "cs"] + [("p", "part", "cs", "part")]
    _, res, _ = evaluate(nodes, edges)
    assert "assembly coordinate system" in res["f"]["message"]


def test_hardening_and_damage_settings_reach_the_material():
    out, bad = script(*box(section={"plastic": "0, 250; 0.1, 300", "hardening": "Kinematic", "fracture_strain": 0.2,
                                    "conductivity": 0.05, "expansion": 1.2e-5}))
    assert not bad, bad
    for text in ("material.Plastic(hardening=KINEMATIC, table=((250.0, 0.0), (300.0, 0.1),))",
                 "material.DuctileDamageInitiation(table=((0.2, 0.0, 0.0),))", "DamageEvolution(type=DISPLACEMENT",
                 "material.Conductivity(table=((0.05,),))", "material.Expansion(table=((1.2e-05,),))"):
        assert text in out["script"], text
    _, res, _ = evaluate(*box(section={"plastic": "0, 250; 0.1, 300; 0.2, 320", "hardening": "Kinematic"}))
    assert "two rows" in res["sec"]["message"]
    out, bad = script(*box(section={"hardening": "Johnson-Cook"}))
    assert not bad, bad
    assert "material.Plastic(hardening=JOHNSON_COOK, table=((350.0, 275.0, 0.36, 1.0, 1800.0, 20.0),))" in out["script"]


def test_rigid_body_point_mass_and_equation_make_their_constraints():
    nodes, edges = box({}, [("f", {"kind": "Fixed"}, "Face", "0, 25, 5")])
    nodes += [("cell_at", "input.value", {"type": "Text", "value": "50, 25, 5"}),
              ("cell", "model.region", {"method": "Find at", "kind": "Cell"}),
              ("v_at", "input.value", {"type": "Text", "value": "100, 50, 10"}),
              ("v", "model.region", {"method": "Find at", "kind": "Vertex"}),
              ("rb", "model.interaction", {"name": "Lump", "kind": "Rigid body", "point": "50, 25, 20"}),
              ("pm", "model.interaction", {"name": "Payload", "kind": "Point mass", "mass": 2, "inertia": "1, 2, 3"}),
              ("eq", "model.interaction", {"name": "Follow", "kind": "Equation", "dof": 3, "ratio": 0.5})]
    edges += [("i", "instance", "cell", "source"), ("cell_at", "value", "cell", "points"),
              ("i", "instance", "v", "source"), ("v_at", "value", "v", "points"),
              ("st", "step", "rb", "step"), ("cell", "region", "rb", "slave"),
              ("st", "step", "pm", "step"), ("rb", "interaction", "pm", "master"),
              ("st", "step", "eq", "step"), ("rb", "interaction", "eq", "master"), ("v", "region", "eq", "slave")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    text = out["script"]
    assert "m.RigidBody(name='Lump', refPointRegion=a.sets['Lump-RP'], bodyRegion=_body)" in text
    assert "a.engineeringFeatures.PointMassInertia(name='Payload', region=mdb.models['M'].rootAssembly.sets['Lump-RP'], " \
           "mass=2.0, i11=1.0, i22=2.0, i33=3.0" in text
    assert "m.Equation(name='Follow', terms=((1.0, 'Follow-A', 3), (-0.5, 'Lump-RP', 3)))" in text


@pytest.mark.parametrize("params, wired, message", [
    ({"kind": "Rigid body"}, {}, "needs a Region (Slave)"),
    ({"kind": "Point mass", "mass": 0}, {"master": "v"}, "Mass must be positive"),
    ({"kind": "Point mass", "inertia": "1, 2"}, {"master": "v"}, "three numbers"),
    ({"kind": "Equation", "dof": 7}, {"master": "v", "slave": "w"}, "1 to 6"),
    ({"kind": "Equation"}, {"master": "v"}, "needs Slave"),
])
def test_rigid_body_point_mass_and_equation_say_what_they_need(params, wired, message):
    nodes, edges = box()
    nodes += [("v_at", "input.value", {"type": "Text", "value": "100, 50, 10"}),
              ("v", "model.region", {"method": "Find at", "kind": "Vertex"}),
              ("w_at", "input.value", {"type": "Text", "value": "100, 0, 10"}),
              ("w", "model.region", {"method": "Find at", "kind": "Vertex"}), ("x", "model.interaction", params)]
    edges += [("i", "instance", "v", "source"), ("v_at", "value", "v", "points"), ("i", "instance", "w", "source"),
              ("w_at", "value", "w", "points"), ("st", "step", "x", "step")]
    edges += [(rid, "region", "x", port) for port, rid in wired.items()]
    _, res, _ = evaluate(nodes, edges)
    assert message in res["x"]["message"], res["x"]
