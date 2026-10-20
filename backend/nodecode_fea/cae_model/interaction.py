"""Contact, ties, couplings and connectors."""

from __future__ import annotations

from .. import stepwise
from ..values import AnalysisStep, Interaction, Region
from .core import HELPERS, _n
from .regions import coupling_point, object_code, region_kw_code, REPOS, selection_code
from .loads import _condition_step_name, model_step_names


def cleanup_lines(name: str) -> list[str]:
    """Inline code removing what an earlier build of this coupling/connector/contact made (scripts run without the
    ledger helpers too)."""
    return HELPERS[HELPERS.index("def _delete_owned"):HELPERS.index("def _delete(key)")].strip().splitlines() + [
        f"_delete_owned(m, {name!r})"]


def _surface(region: Region) -> tuple[str, str]:
    """(repository, side argument) of a surface Region: faces, or a 2D part's edges."""
    sides = "Edges" if region.kind == "edge" else "Faces"
    return sides.lower(), ("side1" if region.side == "Side 1" else "side2") + sides


def _faces_code(region: Region, objects: list[dict], variable: str) -> list[str]:
    return [f"{variable} = {region_kw_code(region, objects, *_surface(region))}"]


def connector_code(value: Interaction, name: str, objects: list[dict]) -> list[str]:
    lines = [f"m = mdb.models[{value.model.name!r}]", "import interaction", "import connectorBehavior",
             "a = m.rootAssembly"] + cleanup_lines(name)
    ends = []
    for i, (end, point) in enumerate(((value.master, value.point), (value.slave, value.point2)), 1):
        if i == 2 and value.ground:
            lines.append("end2 = None")
        elif isinstance(end, Interaction):
            lines.append(f"_set = {coupling_point(end, objects)}")
            lines.append(f"end{i} = _set.referencePoints[0] if len(_set.referencePoints) else _set.vertices[0]")
        elif isinstance(end, Region) and end.kind == "reference point":
            lines.append(f"end{i} = {region_kw_code(end, objects, 'referencePoints', 'referencePoints')}"
                         "['referencePoints'][0]")
        elif isinstance(end, Region):
            selected, _ = selection_code(end, objects, "")
            lines.append(f"end{i} = {selected}[0]")
        else:
            rp = f"{name}-RP{i}"
            lines += [f"_rp = a.ReferencePoint(point={tuple(float(c) for c in point)!r})",
                      f"a.features.changeKey(fromName=_rp.name, toName={rp!r})",
                      f"end{i} = a.referencePoints[_rp.id]"]
        ends.append(f"end{i}")
    wire = name + "-Wire"
    lines += ["_w = a.WirePolyLine(points=((end1, end2),), mergeType=IMPRINT, meshable=False)",
              f"a.features.changeKey(fromName=_w.name, toName={wire!r})",
              f"a.Set(name={wire!r}, edges=a.getFeatureEdges({wire!r}))"]
    kind = {"Beam": "assembledType=BEAM", "Join": "translationalType=JOIN", "Hinge": "assembledType=HINGE",
            "Axial": "translationalType=AXIAL", "Bushing": "assembledType=BUSHING"}[value.connector]
    section = name + "-Section"
    lines.append(f"m.ConnectorSection(name={section!r}, {kind})")
    behaviours = []
    for values, behaviour in ((value.stiffness, "ConnectorElasticity"), (value.damping, "ConnectorDamping")):
        if values:
            components = tuple(c for c, _ in values)
            table = tuple(float(k) for _, k in values)
            behaviours.append(f"connectorBehavior.{behaviour}(components={components!r}, table=({table!r},))")
    if behaviours:
        lines.append(f"m.sections[{section!r}].setValues(behaviorOptions=({', '.join(behaviours)},))")
    lines.append(f"a.SectionAssignment(region=a.sets[{wire!r}], sectionName={section!r})")
    if value.connector in ("Hinge", "Bushing"):  # rotations need axes: the global ones
        csys = name + "-Csys"
        lines += ["_c = a.DatumCsysByDefault(CARTESIAN)", f"a.features.changeKey(fromName=_c.name, toName={csys!r})",
                  f"a.ConnectorOrientation(region=a.sets[{wire!r}], localCsys1=a.datums[_c.id])"]
    return lines


CONTACTS = ("General contact", "Frictional contact", "Frictionless contact")


def explicit_model(model: str, objects: list[dict]) -> bool:
    """Whether the model's steps run in Abaqus/Explicit (it needs its own contact types)."""
    return any(isinstance(o["value"], AnalysisStep) and o["value"].model.name == model and o["value"].explicit
               for o in objects)


def general_contact_code(value: Interaction, name: str, objects: list[dict]) -> list[str]:
    """General contact is made in Initial (CAE refuses it in a later step): the whole model, a face's self-contact,
    or two faces, as named surfaces."""
    lines = [f"m = mdb.models[{value.model.name!r}]", "import interaction", "a = m.rootAssembly"] + cleanup_lines(name)
    try:
        events = interaction_plan(value, objects)
    except ValueError as exc:
        return [f"raise ValueError({str(exc)!r})"]
    prop = name + "-Property"
    lines += [f"if {prop!r} in m.interactionProperties.keys():", f"    del m.interactionProperties[{prop!r}]"]
    lines += _contact_property(prop, value.friction, frictional=bool(value.friction))
    kind = "ContactExp" if explicit_model(value.model.name, objects) else "ContactStd"
    lines.append(f"m.{kind}(name={name!r}, createStepName='Initial')")
    if value.master is None:
        lines.append(f"m.interactions[{name!r}].includedPairs.setValuesInStep(stepName='Initial', useAllstar=ON)")
    else:
        pair = []
        for suffix, face in (("-A", value.master), ("-B", value.slave)):
            if face is None:
                pair.append("SELF")
                continue
            lines += _faces_code(face, objects, "_side")
            lines.append(f"a.Surface(name={name + suffix!r}, **_side)")
            pair.append(f"a.surfaces[{name + suffix!r}]")
        lines.append(f"m.interactions[{name!r}].includedPairs.setValuesInStep(stepName='Initial', useAllstar=OFF, "
                     f"addPairs=(({pair[0]}, {pair[1]}),))")
    lines.append(f"m.interactions[{name!r}].contactPropertyAssignments.appendInStep(stepName='Initial', "
                 f"assignments=((GLOBAL, SELF, {prop!r}),))")
    props = {value.friction: prop}
    for step, what, *rest in events[1:]:  # friction changes: another property from that step on
        friction = rest[0]["friction"]
        if friction not in props:
            props[friction] = f"{name}-Property-{step}"
            lines += _contact_property(props[friction], friction, frictional=bool(friction))
        lines.append(f"m.interactions[{name!r}].contactPropertyAssignments.changeValuesInStep(stepName={step!r}, "
                     f"index=0, value={props[friction]!r})")
    return lines


def _contact_property(prop: str, friction: float, frictional: bool) -> list[str]:
    """A hard, separating contact property with penalty friction (or frictionless)."""
    lines = [f"m.ContactProperty({prop!r})",
             f"m.interactionProperties[{prop!r}].NormalBehavior(pressureOverclosure=HARD, allowSeparation=ON, "
             "constraintEnforcementMethod=DEFAULT)"]
    if frictional:
        lines.append(f"m.interactionProperties[{prop!r}].TangentialBehavior(formulation=PENALTY, "
                     f"directionality=ISOTROPIC, table=(({_n(friction)},),), maximumElasticSlip=FRACTION, "
                     "fraction=0.005)")
    else:
        lines.append(f"m.interactionProperties[{prop!r}].TangentialBehavior(formulation=FRICTIONLESS)")
    return lines


def interaction_plan(value: Interaction, objects: list[dict]) -> list[tuple]:
    """As condition_plan, for contact: (CAE step name, "create" / "change" / "off", {"friction": ...}, copy)."""
    general = value.kind == "General contact"
    start = stepwise.INITIAL if general or value.step is None else value.step.name
    base = {"friction": value.friction}
    if not value.stepwise:
        step_name = start if start == stepwise.INITIAL else _condition_step_name(value.step, objects)
        return [(step_name, "create", base, 0)]
    names, built_names = model_step_names(value.model.name, objects)
    if start not in names:
        names.append(start)
    events = stepwise.plan(names, start, value.active, base, value.changes, value.name, general)
    return [(built_names.get(e[0], e[0]),) + e[1:] for e in events]


def _control_point(value: Interaction, rp: str, objects: list[dict]) -> list[str]:
    """The assembly set rp of a coupling's or rigid body's point: its Master (a reference point or vertex), else
    a reference point made at the typed point."""
    lines = []
    if value.master is not None and value.master.kind == "reference point":
        lines.append(f"a.Set(name={rp!r}, **{region_kw_code(value.master, objects, 'referencePoints', 'referencePoints')})")
    elif value.master is not None:
        selected, _ = selection_code(value.master, objects, "")
        source = object_code(value.master.source, objects)
        lines += [f"picked = {selected}", f"targets = {source}.vertices[0:0]", "for entity in picked:",
                  f"    targets += {source}.vertices[entity.index:entity.index + 1]",
                  f"a.Set(name={rp!r}, vertices=targets)"]
    else:
        lines += [f"_rp = a.ReferencePoint(point={tuple(float(c) for c in value.point)!r})",
                  f"a.features.changeKey(fromName=_rp.name, toName={rp!r})",
                  f"a.Set(name={rp!r}, referencePoints=(a.referencePoints[_rp.id],))"]
    return lines


def coupling_code(value: Interaction, name: str, objects: list[dict]) -> list[str]:
    lines = [f"m = mdb.models[{value.model.name!r}]", "import interaction", "import regionToolset",
             "a = m.rootAssembly"]
    lines += [f"coupled_surface = regionToolset.Region(**{region_kw_code(value.slave, objects, *_surface(value.slave))})"]
    rp = name + "-RP"
    lines += cleanup_lines(name)
    lines += _control_point(value, rp, objects)
    kind = "KINEMATIC" if value.kind == "Kinematic coupling" else "DISTRIBUTING"
    weighting = ", weightingMethod=UNIFORM" if kind == "DISTRIBUTING" else ""
    dofs = ", ".join(f"{d.lower()}={'ON' if d in value.dofs else 'OFF'}"
                     for d in ("U1", "U2", "U3", "UR1", "UR2", "UR3"))
    radius = repr(float(value.influence)) if value.influence else "WHOLE_SURFACE"
    lines.append(f"m.Coupling(name={name!r}, controlPoint=a.sets[{rp!r}], surface=coupled_surface, "
                 f"influenceRadius={radius}, couplingType={kind}{weighting}, localCsys=None, {dofs})")
    return lines


def rigid_body_code(value: Interaction, name: str, objects: list[dict]) -> list[str]:
    """The Slave region moving as one with its reference point: its elements (a region of the part's own
    dimension: cells of a solid, faces of a shell, edges of a wire) or, otherwise, its nodes."""
    from ..values import Instance
    lines = [f"m = mdb.models[{value.model.name!r}]", "import interaction", "import regionToolset",
             "a = m.rootAssembly"] + cleanup_lines(name)
    rp = name + "-RP"
    lines += _control_point(value, rp, objects)
    region = value.slave
    part = region.source.part if isinstance(region.source, Instance) else region.source
    own = {"Solid": "cell", "Shell": "face", "Wire": "edge"}[part.effective_body]
    repo = REPOS[region.kind]
    lines.append(f"_body = regionToolset.Region(**{region_kw_code(region, objects, repo, repo)})")
    which = "bodyRegion" if region.kind == own else "tieRegion"
    lines.append(f"m.RigidBody(name={name!r}, refPointRegion=a.sets[{rp!r}], {which}=_body)")
    return lines


def _point_region(end, objects: list[dict]) -> str:
    """A regionToolset.Region expression of one point: a vertex, a reference point, or a coupling's / rigid
    body's point."""
    if isinstance(end, Interaction):
        return coupling_point(end, objects)
    repo = REPOS[end.kind]
    return f"regionToolset.Region(**{region_kw_code(end, objects, repo, repo)})"


def point_mass_code(value: Interaction, name: str, objects: list[dict]) -> list[str]:
    lines = [f"m = mdb.models[{value.model.name!r}]", "import regionToolset", "a = m.rootAssembly",
             f"if {name!r} in a.engineeringFeatures.inertias.keys():", f"    del a.engineeringFeatures.inertias[{name!r}]"]
    rotary = "".join(f", i{k}{k}={_n(i)}" for k, i in zip((1, 2, 3), value.inertia))
    return lines + [f"a.engineeringFeatures.PointMassInertia(name={name!r}, region={_point_region(value.master, objects)}, "
                    f"mass={_n(value.mass)}{rotary}, alpha=0.0, composite=0.0)"]


def equation_code(value: Interaction, name: str, objects: list[dict]) -> list[str]:
    """Slave's DOF = ratio × Master's: terms (1, slave), (-ratio, master) on one-point assembly sets."""
    lines = [f"m = mdb.models[{value.model.name!r}]", "import interaction", "import regionToolset",
             "a = m.rootAssembly"] + cleanup_lines(name)
    names = []
    for suffix, end in (("-A", value.slave), ("-B", value.master)):
        if isinstance(end, Interaction):  # a coupling's or rigid body's own point set
            names.append(coupling_point(end, objects).rsplit("[", 1)[1].rstrip("]").strip("'\""))
            continue
        repo = REPOS[end.kind]
        lines.append(f"a.Set(name={name + suffix!r}, **{region_kw_code(end, objects, repo, repo)})")
        names.append(name + suffix)
    lines.append(f"m.Equation(name={name!r}, terms=((1.0, {names[0]!r}, {value.dof}), "
                 f"({_n(-value.ratio)}, {names[1]!r}, {value.dof})))")
    return lines


def interaction_code(value: Interaction, name: str, step_name: str, objects: list[dict]) -> list[str]:
    if value.coupling:
        return coupling_code(value, name, objects)
    if value.kind == "Rigid body":
        return rigid_body_code(value, name, objects)
    if value.kind == "Point mass":
        return point_mass_code(value, name, objects)
    if value.kind == "Equation":
        return equation_code(value, name, objects)
    if value.kind == "Connector":
        return connector_code(value, name, objects)
    if value.kind == "General contact":
        return general_contact_code(value, name, objects)
    lines = [f"m = mdb.models[{value.model.name!r}]", "import interaction", "import regionToolset"]
    for label, region in (("master", value.master), ("slave", value.slave)):
        lines += [f"{label}_surface = regionToolset.Region(**{region_kw_code(region, objects, *_surface(region))})"]
    adjust = "ON" if value.adjust else "OFF"
    if value.kind == "Tie":
        lines.append(f"m.Tie({name!r}, master_surface, slave_surface, "
                     f"positionToleranceMethod=COMPUTED, adjust={adjust}, tieRotations=ON, thickness=ON)")
    else:
        try:
            events = interaction_plan(value, objects)
        except ValueError as exc:
            return [f"raise ValueError({str(exc)!r})"]
        frictional = value.kind != "Frictionless contact"
        props = {value.friction: name + "-Property"}
        lines += _contact_property(props[value.friction], value.friction, frictional)
        sliding = "FINITE" if value.sliding == "Finite" else "SMALL"
        current = name
        for step, what, *rest in events:
            if what == "off":
                lines.append(f"m.interactions[{current!r}].deactivate(stepName={step!r})")
                continue
            friction = rest[0]["friction"]
            if friction not in props:
                props[friction] = f"{name}-Property-{step}"
                lines += _contact_property(props[friction], friction, frictional and bool(friction))
            if what == "change":
                lines.append(f"m.interactions[{current!r}].setValuesInStep(stepName={step!r}, "
                             f"interactionProperty={props[friction]!r})")
                continue
            current = stepwise.copy_name(name, step, rest[1])
            if explicit_model(value.model.name, objects):  # Explicit has no small-sliding thickness/adjust options
                lines.append(f"m.SurfaceToSurfaceContactExp({current!r}, {step!r}, master_surface, slave_surface, "
                             f"sliding={sliding}, interactionProperty={props[friction]!r})")
                continue
            lines.append(f"m.SurfaceToSurfaceContactStd({current!r}, {step!r}, master_surface, slave_surface, "
                         f"sliding={sliding}, thickness=ON, interactionProperty={props[friction]!r}, "
                         f"adjustMethod={'OVERCLOSED' if value.adjust else 'NONE'})")
    return lines
