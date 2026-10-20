"""What the Properties node can read from a value: properties(v) -> {name: value}, in display order.

Each property is a value that flows on (Number, Text, Bool, a list of them, or another object such as a Step's
Model or Previous step, so Properties nodes chain). A new property is one line here; no node changes.
"""

from __future__ import annotations

import os

from .values import (AnalysisStep, Bool, Condition, DatumPlane, EntitySet, Field, Instance, Interaction, Job,
                     Material, Model, Number, Odb, OutputRequests, Part, PartOperation, Region, Section, Series,
                     Sketch, Text)


def _t(v, label: str) -> Text:
    return Text("" if v is None else str(v), label)


def _n(v, label: str) -> Number:
    return Number(float(v), label)


def _texts(values, label: str) -> list:
    return [Text(str(v), label) for v in values]


def _model_of(v) -> Model | None:
    for get in (lambda: v.model, lambda: v.part.model, lambda: v.source.model, lambda: v.source.part.model):
        try:
            m = get()
        except (AttributeError, ValueError):
            continue
        if isinstance(m, Model):
            return m
    return None


def _odb(v: Odb) -> dict:
    steps, info = v.info["steps"], v.info
    last = steps[-1] if steps else {"frames": [], "fields": []}
    return {
        "Name": _t(os.path.splitext(os.path.basename(v.path.split("?")[0]))[0], "ODB name"),
        "Path": _t(v.path, "ODB path"),
        "Steps": _texts([s["name"] for s in steps], "step"),
        "Step count": _n(len(steps), "steps"),
        "Last step": _t(steps[-1]["name"] if steps else "", "last step"),
        "Frames in last step": _n(len(last["frames"]), "frames"),
        "Last time": _n(last["frames"][-1]["value"] if last["frames"] else 0.0, "time"),
        "Field outputs": _texts([f["name"] for f in last["fields"]], "field output"),
        "Instances": _texts([i["name"] for i in info.get("instances", [])], "instance"),
        "Nodes": _n(sum(i.get("nodes", 0) for i in info.get("instances", [])), "nodes"),
        "Elements": _n(sum(i.get("elements", 0) for i in info.get("instances", [])), "elements"),
        "Sets": _texts([s["name"] for s in info.get("sets", [])], "set"),
    }


def _field(v: Field) -> dict:
    return {
        "Name": _t(v.name, "field"),
        "Variable": _t(v.origin.get("variable", v.name.split()[0] if v.name else ""), "variable"),
        "Step": _t(v.step, "step"),
        "Frame": _n(v.frame, "frame"),
        "Time": _n(v.frame_value, "time"),
        "Position": _t("Nodal" if v.position == "NODAL" else "Element", "position"),
        "Components": _texts(v.components, "component"),
        "Rows": _n(len(v.labels), "rows"),
        "ODB": v.odb,
    }


def _set(v: EntitySet) -> dict:
    return {
        "Name": _t(v.name, "set"),
        "Type": _t(v.kind.title(), "set type"),
        "Count": _n(len(v.labels), "members"),
        "Labels": [Number(float(x), f"{v.kind} label") for x in v.labels],
        "Instances": _texts(sorted({str(i) for i in v.instances}), "instance"),
    }


def _series(v: Series) -> dict:
    n = len(v.x)
    return {
        "Name": _t(v.name, "curve"),
        "X label": _t(v.xlabel, "x label"),
        "Y label": _t(v.ylabel, "y label"),
        "Points": _n(n, "points"),
        "First x": _n(v.x[0] if n else 0.0, v.xlabel),
        "Last x": _n(v.x[-1] if n else 0.0, v.xlabel),
    }


def _step(v: AnalysisStep) -> dict:
    position, s = 1, v
    while s.previous is not None:
        position, s = position + 1, s.previous
    out = {
        "Name": _t(v.name, "step"),
        "Procedure": _t(v.kind, "procedure"),
        "Model": v.model,
        "Previous step": v.previous if v.previous is not None else _t("Initial", "step"),
        "Previous step name": _t(v.previous.name if v.previous else "Initial", "step"),
        "Position": _n(position, "step number"),
    }
    if v.perturbation:
        return {**out, "Eigenmodes": _n(v.modes, "modes")}
    if v.explicit:
        return {**out, "Time period": _n(v.time, "time period"), "Geometric nonlinearity": Bool(v.nlgeom, "nlgeom"),
                "Time scaling factor": _n(v.scale, "scale factor"),
                "Mass scaling increment": _n(v.mass_dt, "mass scaling increment")}
    return {**out, "Time period": _n(v.time, "time period"), "Geometric nonlinearity": Bool(v.nlgeom, "nlgeom"),
            "Initial increment": _n(v.initial, "initial increment"),
            "Maximum increment": _n(v.maximum, "maximum increment"),
            "Minimum increment": _n(v.minimum, "minimum increment"),
            "Maximum increments": _n(v.increments, "increments")}


def _part(v: Part) -> dict:
    out = {"Name": _t(v.name, "part"), "Model": v.model, "Body": _t(v.effective_body, "body"),
           "Method": _t(v.method, "method")}
    if v.method == "Extrude":
        out["Depth"] = _n(v.depth, "depth")
    if v.method == "Revolve":
        out["Angle"] = _n(v.angle, "angle")
    if v.sketch is not None:
        out["Sketch"] = v.sketch
    if v.file:
        out["File"] = _t(v.file, "file")
    return out


def _material(v: Material) -> dict:
    return {"Name": _t(v.name, "material"), "Model": v.model, "Young's modulus": _n(v.young, "E"),
            "Poisson's ratio": _n(v.poisson, "nu"), "Density": _n(v.density, "density")}


def _section(v: Section) -> dict:
    out = {"Name": _t(v.name, "section"), "Type": _t(v.kind, "section type"), "Material": v.material,
           "Material name": _t(v.material.name, "material")}
    return {**out, "Thickness": _n(v.thickness, "thickness")} if v.thickness else out


def _job(v: Job) -> dict:
    return {"Name": _t(v.name, "job"), "Model": v.model, "Runs": _t(v.action, "action"),
            "Directory": _t(v.directory, "directory"), "CPUs": _n(v.cpus, "CPUs"),
            "ODB path": _t(os.path.join(v.directory, v.name + ".odb"), "ODB path")}


def _named(v, what: str, kind: str = "") -> dict:
    """Model objects with a name, a type and a description."""
    out = {"Name": _t(v.name, what)}
    if kind:
        out["Type"] = _t(kind, f"{what} type")
    model = _model_of(v)
    if model is not None:
        out["Model"] = model
    return out


def properties(v) -> dict:
    """Everything the Properties node can read from v, by name (empty for a value without properties)."""
    if isinstance(v, Odb):
        out = _odb(v)
    elif isinstance(v, Field):
        out = _field(v)
    elif isinstance(v, EntitySet):
        out = _set(v)
    elif isinstance(v, Series):
        out = _series(v)
    elif isinstance(v, Number):
        out = {"Value": v, "Label": _t(v.label, "label"), "Where": _t(v.where, "where")}
    elif isinstance(v, Text):
        out = {"Text": v, "Label": _t(v.label, "label"), "Length": _n(len(v.value), "characters")}
    elif isinstance(v, Bool):
        out = {"Value": v, "Label": _t(v.label, "label")}
    elif isinstance(v, AnalysisStep):
        out = _step(v)
    elif isinstance(v, OutputRequests):
        out = {**_step(v.step), "Field outputs": _texts(v.field, "field output"),
               "History outputs": _texts(v.history, "history output"), "Output frames": _n(v.intervals, "frames")}
    elif isinstance(v, Part):
        out = _part(v)
    elif isinstance(v, Material):
        out = _material(v)
    elif isinstance(v, Section):
        out = _section(v)
    elif isinstance(v, Job):
        out = _job(v)
    elif isinstance(v, Model):
        out = {"Name": _t(v.name, "model"), **({"File": _t(v.file, "file")} if v.file else {})}
    elif isinstance(v, Sketch):
        out = {"Name": _t(v.name, "sketch"), "Items": _n(len(v.items), "items"),
               **({"Model": v.model} if v.model is not None else {})}
    elif isinstance(v, Instance):
        out = {**_named(v, "instance"), "Part": v.part, "Part name": _t(v.part.name, "part")}
    elif isinstance(v, Region):
        out = {"Name": _t(v.name or v.summary(), "region"), "Geometry": _t(v.kind.title(), "geometry"),
               "On": v.source, "On name": _t(v.source.name, "part or instance")}
        if not v.dynamic and v.indices:
            out["Count"] = _n(len(v.indices), "members")
    elif isinstance(v, DatumPlane):
        out = {**_named(v, "datum", v.kind), "On": v.part}
    elif isinstance(v, PartOperation):
        out = {**_named(v, "feature", v.kind), "Part": v.part}
    elif isinstance(v, Condition):
        out = {**_named(v, "condition", v.kind), "Step": v.step, "Step name": _t(v.step.name, "step")}
        if v.region is not None:
            out["Region"] = v.region
    elif isinstance(v, Interaction):
        out = _named(v, "interaction", v.kind)
        if v.step is not None:
            out.update({"Step": v.step, "Step name": _t(v.step.name, "step")})
        if v.kind == "General contact" or v.friction:
            out["Friction"] = _n(v.friction, "friction")
    else:
        return {}
    summary = getattr(v, "summary", None)
    if callable(summary):
        out["Description"] = _t(summary(), "description")
    return out


def as_name(v) -> str | None:
    """A model object's name, for text (None: not a named model object)."""
    if isinstance(v, (Model, Sketch, Part, Instance, DatumPlane, PartOperation, AnalysisStep, OutputRequests,
                      Condition, Interaction, Job, Material, Section)):
        return v.name
    if isinstance(v, Region):
        return v.name or v.summary()
    return None
