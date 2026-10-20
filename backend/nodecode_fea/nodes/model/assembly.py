"""Instance and Boolean Instances: placing parts in the assembly."""

from __future__ import annotations

import math
import numpy as np
from dataclasses import replace

from ...values import Instance, MergedPart, Part
from ..core import Param, Port, register
from ..common import _finite
from ..field import parse_points
from .geometry import _abaqus_name, AXES, _model_value_display, _numbered, _same_geometry


ALIGNMENTS = ["Face to face", "Edge to edge", "Coaxial", "Point to point"]
ALIGN_KINDS = {"Face to face": "face", "Edge to edge": "edge", "Coaxial": "face", "Point to point": "vertex"}


def _alignment(inputs, params, part: Part, origin: str) -> tuple | None:
    """The ("align", …) step of an Instance whose Align and To regions are wired, else None."""
    moving, fixed = inputs.get("align"), inputs.get("to")
    if moving is None and fixed is None:
        return None
    if moving is None or fixed is None:
        raise ValueError("Aligning needs both: Align (a face, edge or vertex of this part) and To (one of another "
                         "instance, which stays put)")
    how = str(params.get("how") or "Face to face")
    if how not in ALIGN_KINDS:
        raise ValueError(f"Align by is one of {', '.join(ALIGNMENTS)}")
    mine = (isinstance(moving.source, Part) and _same_geometry(moving.source, part)) or (
        isinstance(moving.source, Instance) and (moving.source.origin or moving.source.name) == origin)
    if not mine:
        raise ValueError("Align is a Region on this instance's part (or on this instance), the geometry that moves")
    if not isinstance(fixed.source, Instance):
        raise ValueError("To is a Region on another instance (the one that stays put), not on a part")
    if (fixed.source.origin or fixed.source.name) == origin:
        raise ValueError("To is on this same instance: pick the geometry of another instance")
    kind = ALIGN_KINDS[how]
    for label, region in (("Align", moving), ("To", fixed)):
        if region.kind != kind:
            raise ValueError(f"{how} needs a {kind} Region in {label} (it has {region.kind}s)")
        if not region.dynamic and len(region.indices or region.points) != 1:
            raise ValueError(f"{label} is one {kind}: its Region selects {len(region.indices or region.points)}")
    clearance = float(params.get("clearance") or 0.0)
    if not math.isfinite(clearance):
        raise ValueError("Clearance must be a finite number")
    return ("align", how, moving, fixed, str(params.get("flip") or "No") == "Yes", clearance)


@register("model.instance", "Instance", "Model",
          "Place a part in its model's assembly, or remove graph-created instances (including a filtered list). "
          "A placed part is turned (rotate) about an axis through Pivot, then moved "
          "(translate, position), then aligned: wire Align (a face, edge or vertex of this part) and To (one of "
          "another instance) to put faces together, line up edges, make a pin coaxial with a hole or join two "
          "points (assembly position constraints, mate). Fed an instance instead of a part, it turns and moves that same instance further "
          "(no copy): chain Instance nodes for several rotations. Parts only appear in the assembly through "
          "Instance; place a part several times with several Instances or a list (positions, angles: one copy each).",
          inputs=[Port("part", "any", "Part or instance", depth=0,
                       help="A part: a new instance of it. An instance: the same instance, turned and moved further."),
                  Port("align", "region", "Align", optional=True,
                       help="A face, edge or vertex of this part (or of this instance) to bring onto *To*, after the "
                            "turn and move: one entity.", when={"action": ["Place / move"]}),
                  Port("to", "region", "To", optional=True,
                       help="The face, edge or vertex of another instance (which stays put) that *Align* goes onto.",
                       when={"action": ["Place / move"]})],
          outputs=[Port("instance", "instance", "Instance")],
          params=[Param("action", "select", "Place / move", "Action", options=["Place / move", "Remove"],
                        help="Remove drops a graph-created Instance from the assembly. It accepts a filtered list; "
                             "deactivate this node to restore the instances."),
                  Param("name", "text", "", "Name", when={"action": ["Place / move"]},
                        help="Empty: the part's name with -1 (an instance keeps its name)"),
                  Param("axis", "select", "Z", "Rotation axis", options=AXES, when={"action": ["Place / move"]}),
                  Param("angle", "number", 0.0, "Rotation (°)", when={"action": ["Place / move"]}),
                  Param("pivot", "text", "0, 0, 0", "Pivot", when={"action": ["Place / move"]},
                        help="A point on the rotation axis, x, y, z: turn about a corner or centre instead of the "
                             "origin"),
                  Param("x", "number", 0.0, "Move x", when={"action": ["Place / move"]}),
                  Param("y", "number", 0.0, "Move y", when={"action": ["Place / move"]}),
                  Param("z", "number", 0.0, "Move z", when={"action": ["Place / move"]}),
                  Param("how", "select", "Face to face", "Align by", options=ALIGNMENTS,
                        when={"action": ["Place / move"], "wired:to": ["*"]},
                        help="Face to face: the faces meet, facing each other (planar faces). Edge to edge: the "
                             "straight edges line up. Coaxial: the cylindrical faces share their axis (a pin in a "
                             "hole). Point to point: the vertices meet."),
                  Param("flip", "select", "No", "Flip", options=["No", "Yes"],
                        when={"action": ["Place / move"], "wired:to": ["*"]},
                        help="Face to face: Yes puts the faces the same way (overlapping) instead of against each "
                             "other. Edge to edge / Coaxial: reverses the direction along the edge or axis."),
                  Param("clearance", "number", 0.0, "Clearance",
                        when={"action": ["Place / move"], "wired:to": ["*"], "how": ["Face to face"]},
                        help="Face to face: the gap left between the faces."),
                  Param("dependency", "select", "Dependent", "Part reference",
                        options=["Dependent", "Independent"],
                        when={"action": ["Place / move"]},
                        help="Dependent follows later Part changes; Independent copies its geometry into the assembly.")],
          display=lambda o, i, p: _model_value_display(o["instance"]))
def model_instance(inputs, params):
    src = inputs["part"]
    if not isinstance(src, (Part, Instance)):
        raise ValueError("Part or instance takes a Part (a new instance) or an Instance (to turn and move it further)")
    if params.get("action") == "Remove":
        if not isinstance(src, Instance):
            raise ValueError("Remove needs an Instance or a filtered list of Instances")
        if src.existing:
            raise ValueError("Remove only deletes Instances created by this graph; an Instance picked from CAE is left intact")
        return {"instance": replace(src, removed=True)}
    if isinstance(src, Instance) and src.removed:
        raise ValueError("This Instance was removed; deactivate its Remove step before placing or moving it")
    _finite(params, {"x": "Move x", "y": "Move y", "z": "Move z", "angle": "Rotation"})
    pivot = parse_points(str(params["pivot"]), 1)[0]
    if len(str(params["pivot"]).split(";")) > 1 or not np.isfinite(pivot).all():
        raise ValueError("Pivot is one point: x, y, z (e.g. 50, 0, 0)")
    steps = []
    if float(params["angle"]):
        steps.append(("rotate", params["axis"], float(params["angle"]), tuple(float(c) for c in pivot)))
    move = (float(params["x"]), float(params["y"]), float(params["z"]))
    space = getattr(src.part if isinstance(src, Instance) else src, "space", "3D")
    if space != "3D" and (move[2] or (float(params["angle"]) and params["axis"] != "Z")):
        raise ValueError(f"A {space.lower()} part stays in the XY plane: move it in x and y, turn it about Z")
    if space == "Axisymmetric" and (move[0] or float(params["angle"])):
        raise ValueError("An axisymmetric part stays on its axis (y): it only moves along y")
    if any(move):
        steps.append(("move", move))
    given = _abaqus_name(params["name"], "")
    origin = (src.origin or src.name) if isinstance(src, Instance) else ""
    align = None if isinstance(src, Instance) and src.existing else _alignment(
        inputs, params, src.part if isinstance(src, Instance) else src, origin or "\0new")
    if align:
        steps.append(align)
    if isinstance(src, Instance) and src.existing:
        if given or steps:
            raise ValueError("A picked CAE instance is an existing reference and cannot be renamed or moved by "
                             "this node (doing so repeatedly during Live sync would move it again)")
        return {"instance": src}
    # Several copies only when a list loops the node (other than over the parts/instances themselves)
    others = set(params.get("__varied__") or ()) - {"part"}
    if isinstance(src, Instance):
        base = given or src.name
        name = _numbered(base, params, model=src.part.model) if (given or others) else base
        return {"instance": Instance(name, src.part, src.steps + tuple(steps), src.origin or src.name,
                                     dependent=src.dependent)}
    part = src
    name = (_numbered(given, params, model=part.model) if given else
            (_numbered(part.name, params, model=part.model) if others else f"{part.name}-1"))
    return {"instance": Instance(name, part, tuple(steps), name,
                                 dependent=str(params.get("dependency") or "Dependent") == "Dependent")}


@register("model.merge_instances", "Boolean Instances", "Model",
          "Boolean-merge Instances or cut one target Instance with one or more tools, producing a new Part. "
          "Choose independently whether the original Instances remain and whether the resulting Part is placed "
          "in the assembly. Inputs accept lists.",
          inputs=[Port("instances", "instance", "Instances", depth=1, multi=True, optional=True,
                       when={"operation": ["Merge"]}),
                  Port("target", "instance", "Target", optional=True, depth=0, when={"operation": ["Cut"]}),
                  Port("tools", "instance", "Cutting instances", depth=1, multi=True, optional=True,
                       when={"operation": ["Cut"]})],
          outputs=[Port("part", "part", "Result part"),
                   Port("instance", "instance", "Result instance", when={"result": ["Place in assembly"]})],
          params=[Param("operation", "select", "Merge", "Operation", options=["Merge", "Cut"]),
                  Param("name", "text", "Merged-Part", "Name"),
                  Param("intersections", "select", "Remove", "Intersections", options=["Remove", "Keep"],
                        when={"operation": ["Merge"]}),
                  Param("originals", "select", "Keep", "Original instances", options=["Keep", "Delete"],
                        help="Keep restores the source Instances after creating the new Part. Delete consumes the "
                             "graph-created source Instances; the resulting Part remains."),
                  Param("result", "select", "Part only", "Result in assembly",
                        options=["Part only", "Place in assembly"],
                        help="Part only leaves the Boolean result ready for later use. Place in assembly also "
                             "creates an Instance at the origin and exposes it as Result instance."),
                  Param("instance_name", "text", "", "Result instance name",
                        when={"result": ["Place in assembly"]},
                        help="Empty: the result Part name followed by -1. Use the Result instance output with "
                             "Instance if it also needs moving, turning or aligning.")],
          display=lambda o, i, p: _model_value_display(o["part"]), lists=True)
def model_merge_instances(inputs, params):
    def flatten(value):
        return [item for group in value for item in flatten(group)] if isinstance(value, list) else [value]

    operation = str(params.get("operation") or "Merge")
    target = inputs.get("target") if operation == "Cut" else None  # a hidden, still-wired Target is not merged
    instances = tuple(flatten(inputs.get("instances") if operation == "Merge" else inputs.get("tools", [])))
    required = 2 if operation == "Merge" else 1
    if len(instances) < required or not all(isinstance(i, Instance) for i in instances):
        raise ValueError(("Merge needs at least two Instances" if operation == "Merge" else
                          "Cut needs one Target and at least one cutting Instance"))
    if operation == "Cut" and not isinstance(target, Instance):
        raise ValueError("Cut needs one Target Instance")
    all_instances = instances if target is None else (target,) + instances
    models = {i.part.model for i in all_instances}
    if len(models) != 1:
        raise ValueError("All merged Instances must belong to the same Model")
    model = all_instances[0].part.model
    keep_originals = str(params.get("originals") or "Keep") != "Delete"
    if not keep_originals and any(i.existing for i in all_instances):
        raise ValueError("Delete only works on Instances created by this graph; an Instance picked from CAE is left "
                         "intact")
    name = _abaqus_name(str(params.get("name") or "Merged-Part"), "Merged-Part")
    body = target.part.effective_body if target is not None else (
        "Solid" if any(i.part.effective_body == "Solid" for i in instances) else instances[0].part.effective_body)
    part = MergedPart(name, model, body, "Merge", instances=instances,
                      keep_intersections=params.get("intersections") == "Keep",
                      keep_originals=keep_originals, operation=operation, target=target)
    out = {"part": part}
    if params.get("result") == "Place in assembly":
        instance_name = _abaqus_name(str(params.get("instance_name") or f"{name}-1"), f"{name}-1")
        out["instance"] = Instance(instance_name, part, origin=instance_name)
    return out
