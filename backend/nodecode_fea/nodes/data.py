"""Source and Data nodes: open ODBs, read steps, frames, field and history outputs, sets."""

from __future__ import annotations

import os
import numpy as np
from collections import OrderedDict

from .. import jobs, ops
from ..sources import NotReady, SourceError, open_source, source_version
from ..values import EntitySet, Field, Number, Odb, Series, Text
from .core import FRAME_RANGE, _frames_in, opt, Param, Port, register, STEP_HELP, _step_hints, _step_or_last
from .common import as_set


# ---------------------------------------------------------------- sources
MAX_FILES = 500


def _odb_path(params: dict, inputs: dict) -> str:
    """The Path input (text) wins over the Path setting."""
    given = inputs.get("path")
    raw = given.value if isinstance(given, Text) else params.get("path", "")
    return str(raw).strip().strip('"')


def odb_from_path(path: str) -> Odb:
    """An ODB from its file path (or mock:// URL)."""
    path = str(path).strip().strip('"')
    if not path:
        raise ValueError("Empty ODB path")
    if not path.startswith("mock://"):  # a Job's ODB path before or while it runs: wait, don't fail
        stem = os.path.splitext(path)[0]
        if jobs.live(path):  # read what the running job has written so far; it updates with each increment
            if not os.path.exists(path):
                raise NotReady(f"Job {os.path.basename(stem)} is running: no results written yet")
            jobs.runner.follow_odb(path)
            try:
                src = open_source(path)
                return Odb(path, source_version(path), src, src.describe())
            except SourceError as exc:
                raise NotReady(f"Job {os.path.basename(stem)} is running: its first results aren't readable "
                               f"yet ({exc})") from exc
        if not os.path.exists(path) and os.path.exists(stem + ".inp"):
            raise NotReady(f"No results yet: Run job {os.path.basename(stem)} (its input file is written)")
    src = open_source(path)
    return Odb(path, source_version(path), src, src.describe())


def _odb_files(path: str) -> list[str] | None:
    """The ODBs a folder or a wildcard path stands for (None for a plain file path)."""
    import glob
    import os
    if path.startswith("mock://"):
        return None
    if os.path.isdir(path):
        path = os.path.join(path, "*.odb")
    if not any(c in path for c in "*?["):
        return None
    found = sorted(p for p in glob.glob(os.path.expanduser(path), recursive=True) if os.path.isfile(p))
    if not found:
        raise ValueError(f"No files match {path}")
    if len(found) > MAX_FILES:
        raise ValueError(f"That's {len(found)} files; the limit is {MAX_FILES}. Use a narrower pattern.")
    return found


def _open_version(params, inputs):
    path = _odb_path(params, inputs)
    try:
        files = _odb_files(path)
    except ValueError:
        return None
    # a running job's ODB reads again when the job ends, even if its last increment didn't touch the file
    if files is not None:
        return [(f, source_version(f), jobs.live(f)) for f in files]
    return source_version(path), not path.startswith("mock://") and jobs.live(path)


@register("odb.open", "Open ODB", "Data",
          "Open an Abaqus output database, or several: a folder opens every .odb in it, and wildcards pick files "
          "(C:/runs/Job-*.odb; C:/runs/**/*.odb includes subfolders). Several ODBs run the rest of the graph once "
          "per ODB. Use mock://cantilever?load=1000 to try it without Abaqus.",
          inputs=[Port("path", "text", "Path", optional=True)],
          outputs=[Port("odb", "odb", "ODB")],
          params=[Param("path", "file", "mock://cantilever", "Path",
                        help="A .odb file, a folder, or a pattern like C:/runs/Job-*.odb (** for subfolders), "
                             "or a mock:// URL")],
          version=_open_version)
def odb_open(inputs, params):
    path = _odb_path(params, inputs)
    files = _odb_files(path)
    if files is None:
        return {"odb": odb_from_path(path)}
    return {"odb": [odb_from_path(f) for f in files]}


@register("source.files", "Files in Folder", "Source",
          "The .odb files in a folder (optionally its subfolders too), as a list of paths. Connect to Open ODB's "
          "Path to run the rest of the graph once per ODB.",
          outputs=[Port("paths", "text", "Paths"), Port("count", "number", "Count")],
          params=[Param("folder", "text", "", "Folder"), Param("pattern", "text", "*.odb", "Pattern",
                                                              help="Which files, e.g. *.odb or Job-*.odb"),
                  Param("subfolders", "select", "No", "Subfolders", options=["No", "Yes"])])
def source_files(inputs, params):
    from pathlib import Path
    folder = Path(str(params["folder"]).strip().strip('"')).expanduser()
    if not str(params["folder"]).strip():
        raise ValueError("Enter a folder")
    if not folder.is_dir():
        raise ValueError(f"Folder not found: {folder}")
    pattern = str(params["pattern"]).strip() or "*.odb"
    found = sorted(folder.rglob(pattern) if params["subfolders"] == "Yes" else folder.glob(pattern))
    found = [p for p in found if p.is_file()]
    if not found:
        raise ValueError(f"No files matching {pattern} in {folder}")
    if len(found) > MAX_FILES:
        raise ValueError(f"That's {len(found)} files; the limit is {MAX_FILES}. Use a narrower pattern.")
    return {"paths": [Text(str(p), p.name) for p in found], "count": Number(float(len(found)), "Files")}


# ------------------------------------------------------------------- data
_field_cache: "OrderedDict[tuple, dict]" = OrderedDict()


def _field_row_keys(f: Field) -> list[tuple]:
    """Stable row identity, including locations that share an element label."""
    ip = f.integration_points if f.integration_points is not None else np.zeros(len(f.labels), dtype=int)
    sp = f.section_points if f.section_points is not None else np.zeros(len(f.labels), dtype=int)
    return list(zip(f.instances.astype(str), f.labels.astype(int), np.asarray(ip, int), np.asarray(sp, int)))


def _point_word(f: Field) -> str:
    """What the per-row point number means: element-nodal rows carry the node, others the integration point."""
    return "node" if f.origin.get("requested_position") == "Element nodal" else "integration point"


def _read_field(odb: Odb, step: dict, frame: int, variable: str, position: str = "Automatic",
                section_point: int = 0, complex_part: str = "Real", phase_angle: float = 0.0) -> dict:
    """Raw field data, cached so time-history nodes don't re-read every frame on each edit."""
    frame = frame % max(len(step["frames"]), 1) if frame < 0 else frame
    key = (odb.path, odb.version, step["name"], frame, variable, position, section_point,
           complex_part, float(phase_angle))
    if key not in _field_cache:
        _field_cache[key] = odb.source.field(step["name"], frame, variable, position, section_point,
                                             complex_part, float(phase_angle))
        while len(_field_cache) > 64:
            _field_cache.popitem(last=False)
    _field_cache.move_to_end(key)
    return _field_cache[key]


def _field_from(odb: Odb, step: str, frame: int, variable: str, position: str = "Automatic",
                section_point: int = 0, complex_part: str = "Real", phase_angle: float = 0.0) -> Field:
    s = _step_or_last(odb, step)
    res = _read_field(odb, s, int(frame), variable, position, section_point, complex_part, phase_angle)
    values = np.asarray(res["values"], dtype=float)
    values = values.reshape(len(res["labels"]), -1)
    nframes = len(s["frames"])
    kind = ops.classify_type(res.get("type", ""), values.shape[1])
    desc = next((fd for fd in s["fields"] if fd["name"] == variable), {})
    return Field(variable, odb, res["position"], list(res["components"]), np.asarray(res["instances"], dtype=object),
                 np.asarray(res["labels"]), values, s["name"], int(frame) % max(nframes, 1),
                 float(res.get("frameValue", 0.0)), kind,
                 {"variable": variable, "refinement": None, "sets": [], "math": complex_part != "Real", "type": kind,
                  "requested_position": position, "section_point": section_point,
                  "complex_part": complex_part, "phase_angle": float(phase_angle)}, desc.get("invariants"),
                 np.asarray(res.get("integrationPoints", np.zeros(len(values))), dtype=int),
                 np.asarray(res.get("sectionPoints", np.zeros(len(values))), dtype=int))


COMBINE_FRAMES = ["One frame", "Max over frames", "Min over frames", "Abs max over frames", "Mean over frames",
                  "Range over frames (max - min)", "Change (last - first)"]


def _combine_frames(fields: list, how: str) -> Field:
    """One field from the same variable at several frames, node by node / element by element."""
    f0 = fields[-1]  # rows and step/frame of the last frame
    key0 = _field_row_keys(f0)
    stack = []
    for f in fields:
        if _field_row_keys(f) == key0:
            stack.append(f.values)
        else:  # rows in another order or missing: line them up, missing ones are NaN
            where = {k: i for i, k in enumerate(_field_row_keys(f))}
            idx = np.array([where.get(k, -1) for k in key0])
            v = np.full(f0.values.shape, np.nan)
            v[idx >= 0] = f.values[idx[idx >= 0]]
            stack.append(v)
    a = np.stack(stack)  # (frames, rows, components)
    with np.errstate(all="ignore"):
        out = {"Max over frames": lambda: np.nanmax(a, axis=0), "Min over frames": lambda: np.nanmin(a, axis=0),
               "Abs max over frames": lambda: np.take_along_axis(
                   a, np.nanargmax(np.abs(np.nan_to_num(a, nan=0.0)), axis=0)[None], axis=0)[0],
               "Mean over frames": lambda: np.nanmean(a, axis=0),
               "Range over frames (max - min)": lambda: np.nanmax(a, axis=0) - np.nanmin(a, axis=0),
               "Change (last - first)": lambda: a[-1] - a[0]}[how]()
    short = how.split(" (")[0].replace(" over frames", "").lower()
    name = f"{f0.name} ({short}, frames {fields[0].frame}-{fields[-1].frame})"
    return f0.with_values(out, name, f0.components, f0.kind, math=True, frames=[f.frame for f in fields],
                          combine=how)


@register("odb.field", "Field Output", "Data",
          "Read a field output (S, U, RF, PEEQ...) at one step and frame (the last ones unless you choose), whole "
          "or as one component or invariant (Quantity), on the whole model or only Where (a Set, a Display Group, "
          "Compare's Passing, a Reduce result…). "
          "Step and Frame also take wires: a step name or number, a frame number, or lists of them (All Steps, "
          "All Frames, For) to read the field at each. Combine frames makes one field from many frames: the max, "
          "min or mean each node/element reaches (an envelope), its range, or the change from first to last.",
          inputs=[Port("odb", "odb", "ODB"), Port("where", "any", "Where", optional=True, depth=1)],
          outputs=[Port("field", "field", "Field")],
          params=[Param("step", "select", "", "Step", help=STEP_HELP),
                  Param("frame", "select", -1, "Frame", help="Frame number in the step, -1 = last. Takes a wire."),
                  Param("variable", "select", "S", "Variable"),
                  Param("position", "select", "Automatic", "Position",
                        options=["Automatic", "Nodal", "Centroid", "Integration point", "Element nodal"],
                        help="Automatic preserves the previous behavior: nodal values stay nodal; element values "
                             "use whole-element/centroid values or average their integration points."),
                  Param("section_point", "int", 0, "Section point",
                        help="0 keeps all section points; otherwise select the numbered shell/beam/composite point.",
                        when={"position": ["Integration point", "Element nodal"]}),
                  Param("complex", "select", "Real", "Complex result",
                        options=["Real", "Imaginary", "Magnitude", "Phase", "At phase angle"],
                        help="For complex steady-state/modal fields. Real is unchanged for ordinary fields."),
                  Param("phase_angle", "number", 0.0, "Phase angle (°)",
                        when={"complex": ["At phase angle"]}),
                  Param("quantity", "select", "", "Quantity",
                        help="All components (the whole field), or one component / invariant, like a Quantity node"),
                  Param("combine", "select", "One frame", "Combine frames", options=COMBINE_FRAMES,
                        help="One frame: the Frame above. Otherwise one field from the frames From…To (every nth): "
                             "per node/element max, min, abs max, mean, range, or last minus first"),
                  *FRAME_RANGE],
          hints=lambda i, p: _field_output_hints(i.get("odb"), p))
def odb_field(inputs, params):
    from .field import field_filter, field_quantity
    position = str(params.get("position") or "Automatic")
    complex_part = str(params.get("complex") or "Real")
    # Hidden settings keep their last value: only use them where they're shown, as the other options ignore them.
    section_point = int(params.get("section_point") or 0) if position in ("Integration point", "Element nodal") else 0
    phase_angle = float(params.get("phase_angle") or 0.0) if complex_part == "At phase angle" else 0.0

    def read(frame: int) -> Field:
        f = _field_from(inputs["odb"], params["step"], frame, params["variable"],
                        position, section_point, complex_part, phase_angle)
        # invariants before combining: the max of Mises, not the Mises of the max components
        return field_quantity({"field": f}, {"quantity": params["quantity"]})["field"] if params.get("quantity") else f

    if params.get("combine", "One frame") == "One frame":
        f = read(int(params["frame"]))
    else:
        frames = _frames_in(_step_or_last(inputs["odb"], params["step"]), params)
        if len(frames) < 2 and params["combine"] != "Change (last - first)":
            raise ValueError("Combine frames needs at least two frames (check From / To frame)")
        f = _combine_frames([read(fr["index"]) for fr in frames], params["combine"])
    where = as_set(inputs.get("where"))
    if where is not None:  # only on these nodes/elements (in CAE too: the contour shows just them)
        f = field_filter({"field": f, "set": where}, {})["field"]
    return {"field": f}


def _field_output_hints(odb, params):
    h = _step_hints(odb, params)
    if odb is None:
        return h
    try:
        step = _step_or_last(odb, params.get("step", ""))
    except ValueError:
        return h
    fdesc = next((f for f in step["fields"] if f["name"] == params.get("variable")), None)
    if fdesc is not None:
        kind = ops.classify_type(fdesc["type"], len(fdesc["components"]))
        invs = ops.available_invariants(kind, fdesc.get("invariants"))
        h["quantity"] = ([opt("", "All components")] + [opt(x, f"{x} (invariant)") for x in invs]
                         + ([opt(c) for c in fdesc["components"]] if len(fdesc["components"]) > 1 else []))
    return h


PROBLEM_SETS = "Warn*, Err*"


def _set_names(odb: Odb, pattern: str) -> list[str] | None:
    """The ODB's sets a name with wildcards stands for (several patterns separated by commas; any case), or None
    for a plain name."""
    import fnmatch
    patterns = [p.strip() for p in pattern.split(",") if p.strip()]
    if not any(c in pattern for c in "*?[,"):
        return None
    return [s["name"] for s in odb.info["sets"]
            if any(fnmatch.fnmatch(s["name"].lower(), p.lower()) for p in patterns)]


@register("odb.set", "Set", "Data",
          "A node or element set: one defined in the ODB (choose it), or your own nodes/elements, typed as labels "
          "or picked in the Abaqus/CAE viewport (Pick in CAE, then Done there; needs Launch CAE). Wildcards give "
          "every matching set (one each): Warn*, Err* are the nodes and elements Abaqus warned about or failed on "
          "(distorted elements, overconstrained nodes, excessive rotation…), to show or highlight them.",
          inputs=[Port("odb", "odb", "ODB"), Port("name", "text", "Name", optional=True)],
          outputs=[Port("set", "set", "Set")],
          params=[Param("name", "select", "", "Set",
                        help="A set of the ODB, or a pattern with * (Warn*, Err*: the problem sets Abaqus makes). "
                             "Leave empty to use Labels."),
                  Param("kind", "select", "node", "Labels are", options=["node", "element"]),
                  Param("labels", "text", "", "Labels",
                        help="Your own members, typed (BEAM-1.12, 13, 20:25) or filled in by Pick in CAE"),
                  Param("pick", "pick", "", "", help="Pick in the CAE viewport")],
          hints=lambda i, p: {"name": [opt(PROBLEM_SETS, "Warnings and errors (Warn*, Err*)")] + [
              opt(s["name"], f"{s['name']} ({s['kind']})") for s in i["odb"].info["sets"]]} if i.get("odb") else {})
def odb_set(inputs, params):
    from .field import cae_pick
    name = inputs["name"].value.strip() if inputs.get("name") is not None else params["name"]
    if str(params.get("labels") or "").strip():
        if name:
            raise ValueError("Choose a set or type labels, not both")
        return cae_pick(inputs, params)
    if not name:
        raise ValueError("Choose a set, type labels, or Pick in CAE")
    names = _set_names(inputs["odb"], name)
    if names is not None:
        if not names:
            problems = name.replace(" ", "") == PROBLEM_SETS.replace(" ", "")
            raise ValueError(f"No set matches {name}" + (": Abaqus made no warning or error sets in this ODB"
                                                         if problems else ""))
        sets = []
        for n in names:
            res = inputs["odb"].source.set(n)
            sets.append(EntitySet(n, res["kind"], np.asarray(res["instances"], dtype=object),
                                  np.asarray(res["labels"])))
        return {"set": sets}
    res = inputs["odb"].source.set(name)
    return {"set": EntitySet(name, res["kind"], np.asarray(res["instances"], dtype=object), np.asarray(res["labels"]))}


@register("odb.history", "History Output", "Data", "A history output curve (e.g. RF2 at a reference point), "
          "from one step or concatenated across all steps on accumulated total time.",
          inputs=[Port("odb", "odb", "ODB")], outputs=[Port("series", "series", "Series")],
          params=[Param("steps", "select", "Selected step", "Steps", options=["Selected step", "All steps"]),
                  Param("step", "select", "", "Step", help=STEP_HELP,
                        when={"steps": ["Selected step"]}), Param("region", "select", "", "Region"),
                  Param("output", "select", "", "Output")],
          hints=lambda i, p: _step_hints(i.get("odb"), p))
def odb_history(inputs, params):
    odb = inputs["odb"]
    if not params["region"] or not params["output"]:
        raise ValueError("Choose a region and an output")
    steps = odb.info["steps"] if params.get("steps") == "All steps" else [_step_or_last(odb, params["step"])]
    xs, ys, offset = [], [], 0.0
    for step in steps:
        try:
            res = odb.source.history(step["name"], params["region"], params["output"])
        except Exception as exc:
            if len(steps) == 1:
                raise
            raise ValueError(f"{params['output']} @ {params['region']} is unavailable in step "
                             f"'{step['name']}': {exc}") from exc
        x, y = np.asarray(res["x"], float), np.asarray(res["y"], float)
        # Abaqus gives each step's start on the total-time axis; without it, continue from the last point.
        if "totalTime" in step:
            offset = float(step["totalTime"])
        xs.append(x + offset)
        ys.append(y)
        if len(x):
            offset += float(x[-1])
    label = f"{params['output']} @ {params['region']}"
    return {"series": Series(label, np.concatenate(xs) if xs else np.zeros(0),
                             np.concatenate(ys) if ys else np.zeros(0),
                             "Total time" if len(steps) > 1 else "Step time", params["output"])}
