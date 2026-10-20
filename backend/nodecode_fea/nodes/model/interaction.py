"""Interaction: contact, ties, couplings and connectors."""

from __future__ import annotations

import math
import re

from ... import stepwise
from ...values import AnalysisStep, Instance, Interaction, Region
from ..core import Param, Port, register
from ..common import _region_model
from ..field import parse_points
from .geometry import _abaqus_name, _model_value_display, _numbered
from .loads import _check_stepwise, _condition_number, _step_table, STEP_TABLE_HELP


INTERACTION_KINDS = ["Tie", "Frictionless contact", "Frictional contact", "General contact", "Kinematic coupling",
                     "Distributing coupling", "Connector", "Rigid body", "Point mass", "Equation"]
COUPLINGS = ["Kinematic coupling", "Distributing coupling"]
CONNECTORS = ["Beam", "Join", "Hinge", "Axial", "Bushing"]
# the relative motions each connector leaves free: what a stiffness can act on (1-3 translations, 4-6 rotations)
CONNECTOR_FREE = {"Beam": (), "Join": (4, 5, 6), "Hinge": (4,), "Axial": (1,), "Bushing": (1, 2, 3, 4, 5, 6)}


def _stiffness(text: str, connector: str, what: str = "stiffness") -> tuple:
    """((component, stiffness or damping), ...): one number for every free component, or one each."""
    values = [float(c) for c in re.split(r"[,;\s]+", text.strip()) if c]
    if not values:
        return ()
    free = CONNECTOR_FREE[connector]
    if not free:
        raise ValueError(f"A {connector} connector is rigid: it has no free motion for a {what}")
    if len(values) == 1:
        values = values * len(free)
    if len(values) != len(free):
        raise ValueError(f"A {connector} connector takes one {what}, or {len(free)} (one per free component "
                         f"{', '.join(map(str, free))})")
    if not all(math.isfinite(k) and k >= 0 for k in values):
        raise ValueError(f"Connector {what} must be a finite number, 0 or more")
    return tuple(zip(free, values))


def _connector_end(value, label: str, model):
    """A connector end: a Vertex Region on an instance, a coupling (its point), or None (a typed point)."""
    if value is None:
        return None
    if isinstance(value, Interaction) and (value.point_owner or value.kind == "Connector"):
        if value.kind == "Connector":
            raise ValueError(f"{label}: a connector ends at a vertex or a coupling's point, not another connector")
        if model is not None and value.model != model:
            raise ValueError(f"{label} belongs to another model")
        return value
    if isinstance(value, Region) and value.kind == "reference point":
        if model is not None and _region_model(value) != model:
            raise ValueError(f"{label} belongs to another model")
        return value
    if isinstance(value, Region) and value.kind == "vertex":
        if not isinstance(value.source, Instance):
            raise ValueError(f"{label} needs a Vertex Region on an Instance (the assembly), not on a Part")
        if model is not None and _region_model(value) != model:
            raise ValueError(f"{label} belongs to another model")
        return value
    raise ValueError(f"{label} takes a Vertex Region, a reference point (Part Feature) or a coupling (its reference "
                     "point); unwired, the typed point")


def _surface_kind(region: Region) -> str:
    """What a surface is made of: faces, or a 2D part's edges."""
    part = region.source.part if isinstance(region.source, Instance) else region.source
    return "edge" if getattr(part, "flat", False) else "face"


DOF_NAMES = ("U1", "U2", "U3", "UR1", "UR2", "UR3")


def _dofs(text: str) -> tuple:
    raw = [w.upper() for w in re.split(r"[,;\s]+", text.strip()) if w]
    if not raw or raw == ["ALL"]:
        return DOF_NAMES
    bad = [w for w in raw if w not in DOF_NAMES]
    if bad:
        raise ValueError(f"Coupled DOFs: unknown '{bad[0]}' (use All or U1, U2, U3, UR1, UR2, UR3)")
    return tuple(d for d in DOF_NAMES if d in raw)


def _friction_steps(params: dict, kind: str, table=None) -> tuple[float, tuple, tuple]:
    """Contact's friction where it starts, its changes by step, and the steps a pair contact is active in (typed,
    or from a wired Per step table)."""
    base, steps = stepwise.split(params.get("friction", 0.2))
    friction = _condition_number(base if base != "" else 0.2, "Friction coefficient")
    changes = tuple((s, _condition_number(v, f"Friction coefficient in {s}")) for s, v in steps)
    active = stepwise.parse_active(params.get("active") or "")
    if table is not None:
        table_active, table_changes = _step_table(
            table, {"active": "active", "friction": "friction", "friction coefficient": "friction"},
            lambda key, text, label: _condition_number(text, label))
        active = table_active if table_active is not None else active
        changes += next((seq for k, seq in table_changes if k == "friction"), ())
    if friction < 0 or any(v < 0 for _, v in changes):
        raise ValueError("Friction coefficient cannot be negative")
    if changes and kind not in ("Frictional contact", "General contact"):
        raise ValueError(f"{kind} has no friction to change by step: use Frictional contact (0 is frictionless)")
    if active and kind not in ("Frictionless contact", "Frictional contact"):
        raise ValueError(f"Active in is for pair contact: {'general contact covers every step' if kind == 'General contact' else 'ties, couplings and connectors are model constraints, in every step'}")
    return friction, (("friction", changes),) if changes else (), active


@register("model.interaction", "Interaction", "Model",
          "Connect two face Regions with a Tie or surface contact, let everything touch everything (general "
          "contact, self-contact), couple a face to a reference point (kinematic or distributing coupling, RBE2 / "
          "RBE3-like, a remote point), or join two points with a connector (a rigid beam link, pin/join, hinge, "
          "axial spring, bushing; bolts, springs and joints, with stiffness), including a point-to-ground support; "
          "make a region rigid (Rigid body: it moves as one with a reference point), add a lumped Point mass with "
          "rotary inertia at a point (a motor, a payload, added mass), or tie one point's degree of freedom to "
          "another's (Equation, MPC: slave = ratio × master). "
          "Wire a coupling into a Load / "
          "Boundary Condition's Region to load or hold its reference point, or a coupling or connector into a "
          "Step's History region to record RF/U or CTF/CU there. Feed another Interaction to add several; settings "
          "are wireable and lists make interaction families.",
          inputs=[Port("step", "any", "Step or previous interaction", optional=True, depth=0,
                       help="Pair contact needs a Step; ties, couplings, connectors and general contact (always "
                            "made in Initial) need none, but a Step tells them the model."),
                  Port("master", "any", "Master", optional=True, depth=0,
                       help="Tie and contact: the master face. Coupling: optionally a vertex to use as the control "
                            "point. Connector: its first end (a vertex or a coupling). General contact: optionally "
                            "a face to restrict it to. Rigid body: optionally its reference point (Part Feature). "
                            "Point mass: where it is (a vertex, a reference point, a coupling or rigid body). "
                            "Equation: the independent point."),
                  Port("slave", "any", "Slave", optional=True, depth=0,
                       help="Tie and contact: the slave face. Coupling: the face(s) the point drives. Connector: "
                            "its second end; leave unwired and choose Ground for a grounded support. General "
                            "contact: optionally a second face (Master with Slave). Rigid body: the region made "
                            "rigid. Equation: the dependent point (its DOF follows Master's)."),
                  Port("steps", "any", "Per step", optional=True, depth=2, table=True,
                       when={"kind": ["Frictionless contact", "Frictional contact", "General contact"]},
                       help=STEP_TABLE_HELP + " Contact: Step, Active, Friction.")],
          outputs=[Port("interaction", "interaction", "Interaction")],
          params=[Param("name", "text", "Interaction-1", "Name"),
                  Param("kind", "select", "Tie", "Kind", options=INTERACTION_KINDS),
                  Param("point", "text", "0, 0, 0", "Reference point",
                        when={"kind": COUPLINGS + ["Connector", "Rigid body"], "unwired:master": ["1"]},
                        help="x, y, z of the coupling's reference point, or the connector's first end (unless Master "
                             "is wired)"),
                  Param("point_from", "select", "Coordinates", "Reference point at",
                        options=["Coordinates", "Slave Region point"],
                        when={"kind": COUPLINGS, "unwired:master": ["1"]},
                        help="Coordinates uses Reference point. Slave Region point uses the locating point of its "
                             "one-face Slave; a By-shape Region set to One per geometry supplies each face centre."),
                  Param("second", "select", "Point", "Second end", options=["Point", "Ground"],
                        when={"kind": ["Connector"], "unwired:slave": ["1"]},
                        help="Point uses Second point; Ground attaches the connector directly to fixed ground."),
                  Param("point2", "text", "10, 0, 0", "Second point",
                        when={"kind": ["Connector"], "second": ["Point"], "unwired:slave": ["1"]},
                        help="x, y, z of the connector's second end (unless Slave is wired)"),
                  Param("connector", "select", "Beam", "Connector", options=CONNECTORS, when={"kind": ["Connector"]},
                        help="Beam: rigid link. Join: pinned (rotations free). Hinge: turns about global X. Axial: "
                             "free along the line (a spring with stiffness). Bushing: all six free (springs)."),
                  Param("damping", "text", "", "Damping", when={"kind": ["Connector"]},
                        help="Viscous damping of the free components (force per velocity, a dashpot or damper): one "
                             "value for all, or one each, like Stiffness. Empty: none."),
                  Param("stiffness", "text", "", "Stiffness", when={"kind": ["Connector"]},
                        help="Elastic stiffness of the free components: one value for all, or one each (Bushing: "
                             "3 forces/length then 3 moments/radian). Empty: free."),
                  Param("mass", "number", 1.0, "Mass", when={"kind": ["Point mass"]},
                        help="In the model's units (tonne with mm, N, MPa; kg with m, N, Pa)."),
                  Param("inertia", "text", "0, 0, 0", "Rotary inertia", when={"kind": ["Point mass"]},
                        help="I11, I22, I33 about global X, Y, Z (mass × length²). 0, 0, 0: a point mass only."),
                  Param("dof", "int", 1, "Degree of freedom", when={"kind": ["Equation"]},
                        help="1, 2, 3: displacements U1–U3; 4, 5, 6: rotations UR1–UR3."),
                  Param("ratio", "number", 1.0, "Ratio", when={"kind": ["Equation"]},
                        help="Slave's DOF = Ratio × Master's (1: they move together in that direction; -1: "
                             "mirrored)."),
                  Param("dofs", "text", "All", "Coupled DOFs", when={"kind": COUPLINGS},
                        help="All, or some of U1, U2, U3, UR1, UR2, UR3"),
                  Param("influence", "number", 0, "Radius of influence", when={"kind": COUPLINGS},
                        help="Only the Slave faces' nodes within this distance of the reference point are coupled. "
                             "0: the whole surface."),
                  Param("active", "text", "", "Active in", per_step=True, table_only=True,
                        when={"kind": ["Frictionless contact", "Frictional contact"]},
                        help="The steps the contact acts in: names and ranges, e.g. Assemble to Load, or Load to "
                             "end. Empty: from its Step on. Off in a step removes it there; on again later makes a "
                             "fresh copy (<name>-from-<step>)."),
                  Param("friction", "number", 0.2, "Friction coefficient", per_step=True,
                        when={"kind": ["Frictional contact", "General contact"]},
                        help="General contact: 0 for frictionless. Per step: 0.1; Slide: 0.3 (each value its own "
                             "contact property, changed in that step)."),
                  Param("sliding", "select", "Finite", "Sliding", options=["Finite", "Small"],
                        when={"kind": ["Frictionless contact", "Frictional contact"]}),
                  Param("adjust", "select", "Yes", "Adjust surfaces", options=["Yes", "No"],
                        when={"kind": ["Tie", "Frictionless contact", "Frictional contact"]})],
          display=lambda o, i, p: _model_value_display(o["interaction"]), late=_check_stepwise)
def model_interaction(inputs, params):
    upstream = inputs.get("step")
    previous = upstream if isinstance(upstream, Interaction) else None
    step = upstream.step if previous is not None else upstream
    master, slave, kind = inputs.get("master"), inputs.get("slave"), str(params["kind"])
    if step is None and kind in ("Frictionless contact", "Frictional contact"):
        raise ValueError("Contact needs a Step (or a previous Interaction in one)")
    if step is not None and not isinstance(step, AnalysisStep):
        raise ValueError("Interaction's Step input takes a Step or previous Interaction")
    if step is not None:
        step_model = step.model
    else:  # the Regions (or couplings) decide the model
        ends = [e for e in (slave, master) if isinstance(e, (Region, Interaction))]
        step_model = (None if not ends else ends[0].model if isinstance(ends[0], Interaction) else
                      _region_model(ends[0]))
    name = _numbered(_abaqus_name(str(params["name"]), "Interaction-1"), params, model=step_model)
    if kind == "Rigid body":
        if not isinstance(slave, Region) or slave.kind == "reference point":
            raise ValueError("A rigid body needs a Region (Slave) to make rigid: cells, faces, edges or vertices")
        if master is not None and (not isinstance(master, Region) or master.kind != "reference point"):
            raise ValueError("A rigid body's Master is optional: a reference point (Part Feature); unwired, the "
                             "typed Reference point")
        if _region_model(slave) != step_model or (master is not None and _region_model(master) != step_model):
            raise ValueError("The Regions and the Step must belong to the same model")
        point = () if master is not None else tuple(float(c) for c in parse_points(str(params["point"]), 1)[0][:3])
        if len(point) not in (0, 3):
            raise ValueError("Reference point is x, y, z")
        return {"interaction": Interaction(name, step, kind, master, slave, previous=previous, point=point)}
    if kind in ("Point mass", "Equation"):
        ends = ((master, "Master"),) if kind == "Point mass" else ((master, "Master"), (slave, "Slave"))
        for end, label in ends:
            if end is None:
                raise ValueError(f"{kind} needs {label}: a vertex, a reference point, or a coupling or rigid body "
                                 "(its point)")
            _connector_end(end, label, step_model)
        if kind == "Point mass":
            mass = _condition_number(params.get("mass"), "Mass")
            inertia = tuple(float(c) for c in re.split(r"[,;\s]+", str(params.get("inertia") or "").strip()) if c)
            if mass <= 0:
                raise ValueError("Mass must be positive")
            if inertia and (len(inertia) != 3 or any(not math.isfinite(i) or i < 0 for i in inertia)):
                raise ValueError("Rotary inertia is I11, I22, I33: three numbers, 0 or more")
            return {"interaction": Interaction(name, step, kind, master, None, previous=previous, mass=mass,
                                                inertia=inertia if any(inertia) else ())}
        dof, ratio = int(params.get("dof") or 0), _condition_number(params.get("ratio"), "Ratio")
        if not 1 <= dof <= 6:
            raise ValueError("Degree of freedom is 1 to 6 (U1–U3, UR1–UR3)")
        if master == slave:
            raise ValueError("An equation ties two different points")
        return {"interaction": Interaction(name, step, kind, master, slave, previous=previous, dof=dof, ratio=ratio)}
    if kind == "Connector":
        if step_model is None:
            raise ValueError("A connector between typed points needs a Step or Model to know its model: wire Step")
        first, second = (_connector_end(master, "Master", step_model), _connector_end(slave, "Slave", step_model))

        def typed(key, label):
            pts = parse_points(str(params.get(key) or ""), 1)
            if pts.shape[1] < 3:
                raise ValueError(f"{label} is x, y, z")
            return tuple(float(c) for c in pts[0][:3])

        point = typed("point", "Reference point") if first is None else ()
        ground = second is None and str(params.get("second") or "Point") == "Ground"
        point2 = typed("point2", "Second point") if second is None and not ground else ()
        if first is None and second is None and not ground and point == point2:
            raise ValueError("The connector's two points are the same")
        connector = str(params.get("connector") or "Beam")
        if connector not in CONNECTORS:
            raise ValueError(f"Connector is one of {', '.join(CONNECTORS)}")
        return {"interaction": Interaction(name, step, kind, first, second, previous=previous, point=point,
                                            point2=point2, ground=ground, connector=connector,
                                            stiffness=_stiffness(str(params.get("stiffness") or ""), connector),
                                            damping=_stiffness(str(params.get("damping") or ""), connector, "damping"))}
    if kind == "General contact":
        if step is None:
            raise ValueError("General contact needs a Step (it is made in Initial; the Step tells the model)")
        for label, face in (("Master", master), ("Slave", slave)):
            if face is not None and (not isinstance(face, Region) or face.kind != _surface_kind(face)):
                raise ValueError(f"General contact's {label} is optional: a Face Region")
            if face is not None and _region_model(face) != step_model:
                raise ValueError(f"General contact's {label} belongs to another model")
        friction, changes, active = _friction_steps(params, kind, inputs.get("steps"))
        if master is None and slave is not None:
            master, slave = slave, None
        return {"interaction": Interaction(name, step, kind, master, slave, friction, previous=previous,
                                            changes=changes)}
    if kind in COUPLINGS:
        if not isinstance(slave, Region) or slave.kind != _surface_kind(slave):
            raise ValueError("A coupling needs a Face Region (Slave) for the point to drive")
        if master is not None and (not isinstance(master, Region) or master.kind not in ("vertex", "reference point")):
            raise ValueError("A coupling's Master is optional: a Vertex Region or a reference point (Part Feature) to "
                             "use as its control point")
        if _region_model(slave) != step_model or (master is not None and _region_model(master) != step_model):
            raise ValueError("The Regions and the Step must belong to the same model")
        if master is not None:
            point = ()
        elif str(params.get("point_from") or "Coordinates") == "Slave Region point" and slave.at:
            point = slave.at
        elif str(params.get("point_from") or "Coordinates") == "Slave Region point":
            if slave.dynamic or slave.indices or len(slave.points) != 1 or len(slave.points[0]) < 3:
                raise ValueError("Slave Region point needs one Face Region located by one x, y, z point (a By-shape "
                                 "Region with Matching geometry = One per geometry supplies this automatically)")
            point = tuple(float(c) for c in slave.points[0][:3])
        else:
            point = tuple(float(c) for c in parse_points(str(params["point"]), 1)[0][:3])
        if len(point) not in (0, 3):
            raise ValueError("Reference point is x, y, z")
        influence = _condition_number(params.get("influence") or 0, "Radius of influence")
        if influence < 0:
            raise ValueError("Radius of influence cannot be negative (0: the whole surface)")
        return {"interaction": Interaction(name, step, kind, master, slave, previous=previous, point=point,
                                            dofs=_dofs(str(params.get("dofs") or "All")), influence=influence)}
    if not isinstance(master, Region) or not isinstance(slave, Region):
        raise ValueError("Interaction needs Master and Slave face Regions")
    if master.kind != _surface_kind(master) or slave.kind != _surface_kind(slave):
        raise ValueError("Interaction needs Face Regions for Master and Slave")
    if _region_model(master) != step_model or _region_model(slave) != step_model:
        raise ValueError("Both Regions and the Step must belong to the same model")
    friction, changes, active = _friction_steps(params, kind, inputs.get("steps"))
    return {"interaction": Interaction(name, step, kind, master, slave, friction,
                                        params["adjust"] == "Yes", str(params["sliding"]), previous,
                                        active=active, changes=changes)}
