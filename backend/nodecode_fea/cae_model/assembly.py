"""Instances, their placement, Boolean merges and jobs."""

from __future__ import annotations

from ..values import Instance, Job, MergedPart, Part
from .core import _n, _pt
from .regions import object_code, selection_code


ALIGN_CALLS = {"Face to face": ("FaceToFace", "movablePlane", "fixedPlane"),
               "Edge to edge": ("EdgeToEdge", "movableAxis", "fixedAxis"),
               "Coaxial": ("Coaxial", "movableAxis", "fixedAxis"),
               "Point to point": ("CoincidentPoint", "movablePoint", "fixedPoint")}


def _align_lines(s: tuple, name: str, part: str, objects: list[dict], k: int) -> list[str]:
    """An alignment step: the moving entity (found on the part, or on this instance as placed so far, and taken
    from this instance by its index) onto the fixed instance's, as a renamed assembly position constraint."""
    _, how, moving, fixed, flip, clearance = s
    repo = {"face": "faces", "edge": "edges", "vertex": "vertices"}[moving.kind]
    owner = f"m.parts[{part!r}]" if isinstance(moving.source, Part) else f"a.instances[{name!r}]"
    if moving.indices:
        found = f"tuple({owner}.{repo}[i] for i in {tuple(moving.indices)!r})"
    else:
        found = (f"_find_geometry({owner}.{repo}, {tuple(moving.points)!r}, {_n(moving.tolerance)}, "
                 f"'Align region', owner={owner})")
    fixed_found, _ = selection_code(fixed, objects, "")
    method, mover, target = ALIGN_CALLS[how]
    # Face to face: Abaqus' flip=ON is the faces against each other (OFF overlaps them)
    on = (not flip) if how == "Face to face" else flip
    extra = f", clearance={_n(clearance)}" if how == "Face to face" else ""
    flipped = "" if how == "Point to point" else f", flip={'ON' if on else 'OFF'}"
    return [f"_moving = _geometry({found}, 'Align region')[0]",
            f"_moving = a.instances[{name!r}].{repo}[_moving.index]",
            f"_fixed = _geometry({fixed_found}, 'To region')[0]",
            f"_feature = a.{method}({mover}=_moving, {target}=_fixed{flipped}{extra})",
            f"a.features.changeKey(fromName=_feature.name, toName={name + '-Align-' + str(k)!r})"]


def instance_code(i: Instance, name: str | None = None, part: str | None = None,
                  objects: list[dict] | None = None) -> list[str]:
    """The instance, then each of its steps in order (rotations about an axis through their pivot, moves,
    alignments onto other instances)."""
    name, part = name or i.name, part or i.part.name
    lines = [f"m = mdb.models[{i.part.model.name!r}]", "a = m.rootAssembly",
             f"a.Instance(name={name!r}, part=m.parts[{part!r}], dependent={'ON' if i.dependent else 'OFF'})"]
    aligned = 0
    for s in i.steps:
        if s[0] == "align":
            aligned += 1
            lines += _align_lines(s, name, part, objects or [], aligned)
        elif s[0] == "rotate":
            axis = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}[s[1]]
            lines.append(f"a.rotate(instanceList=({name!r},), axisPoint={_pt(s[3])}, axisDirection={axis!r}, "
                         f"angle={_n(s[2])})")
        else:
            lines.append(f"a.translate(instanceList=({name!r},), vector={_pt(s[1])})")
    return lines


def _superseded(v: Instance, values: dict) -> bool:
    """An instance a later step of its chain continues (reached through a list node, say): that one is built."""
    return any(isinstance(w, Instance) and w.origin == v.origin and w.part == v.part
               and ((w.removed and w.steps == v.steps)
                    or (len(w.steps) > len(v.steps) and w.steps[:len(v.steps)] == v.steps))
               for vals in values.values() for w in vals)


def merge_code(value: MergedPart, name: str, objects: list[dict]) -> list[str]:
    refs = [object_code(instance, objects) for instance in value.instances]
    used = list(value.instances)
    originals = "SUPPRESS" if value.keep_originals else "DELETE"
    if value.operation == "Cut":
        target = object_code(value.target, objects)
        used.insert(0, value.target)
        operation = (f"_merged = a.InstanceFromBooleanCut(name={name!r}, instanceToBeCut={target}, "
                     f"cuttingInstances=({', '.join(refs)},), originalInstances={originals})")
    else:
        operation = (f"_merged = a.InstanceFromBooleanMerge(name={name!r}, instances=({', '.join(refs)},), "
                     f"keepIntersections={'ON' if value.keep_intersections else 'OFF'}, "
                     f"originalInstances={originals}, domain=GEOMETRY)")
    names = [next(o["name"] for o in objects if o["value"] == instance) for instance in used]
    restore = [f"a.features[{instance_name!r}].resume()" for instance_name in names] if value.keep_originals else []
    return [f"m = mdb.models[{value.model.name!r}]", "a = m.rootAssembly", operation,
            *restore, "del a.features[_merged.name]"]


def job_code(value: Job, name: str) -> list[str]:
    """Define a Job and write its input file, without leaving CAE in a different working directory. CAE never
    submits it: the editor runs it (Run on the Job node), so editing the model doesn't restart an analysis."""
    return [f"_job_dir = {value.directory!r}",
            f"if os.path.exists(os.path.join(_job_dir, {name + '.lck'!r})):",
            f"    raise ValueError('job {name} is running: its input is rewritten once it finishes or is killed')",
            "_old_cwd = os.getcwd()",
            "if not os.path.isdir(_job_dir): os.makedirs(_job_dir)", "os.chdir(_job_dir)", "try:",
            f"    _job = mdb.Job(name={name!r}, model={value.model.name!r}, numCpus={value.cpus}, "
            f"numDomains={value.cpus}, memory={value.memory}, memoryUnits=PERCENTAGE"
            + (f", userSubroutine={value.user!r})" if value.user else ")"),
            "    _job.writeInput(consistencyChecking=OFF)", "finally:", "    os.chdir(_old_cwd)"]
