"""Loop nodes and properties: For, For Each, and lists flowing through nodes."""

from __future__ import annotations

import re

import numpy as np

from ..properties import as_name, properties
from ..values import Bool, EntitySet, Field, Number, Odb, Series, Text
from .core import _fmt, FRAME_RANGE, _frames_in, opt, Param, Port, register, STEP_HELP, _step_hints, _step_or_last
from .common import _as_list, _as_text, _finite, _is_collection, _size
from .maths import series_xy
from .lists import MAX_SPLIT, _member, _take


# ------------------------------------------------------------------ loops
MAX_RANGE = 10000


@register("odb.timeline", "Steps & Frames", "Data",
          "What the ODB holds over time: the step names (connect to a Step input to loop over steps), how many "
          "frames each step has, and the frame numbers and times of one step (From–To, every nth; connect Frames "
          "to Field Output's Frame to loop over frames). Times are a Frequency step's natural frequencies and a "
          "Buckling step's eigenvalues (critical load factors; From frame 1 skips its base state).",
          inputs=[Port("odb", "odb", "ODB")],
          outputs=[Port("steps", "text", "Steps"), Port("nframes", "number", "Frames per step"),
                   Port("frames", "number", "Frames"), Port("times", "number", "Times")],
          params=[Param("step", "select", "", "Step", help=STEP_HELP), *FRAME_RANGE],
          hints=lambda i, p: _step_hints(i.get("odb"), p))
def odb_timeline(inputs, params):
    steps = odb_steps(inputs, params)
    frames = odb_frames(inputs, params)
    return {"steps": steps["names"], "nframes": steps["frames"], "frames": frames["frames"], "times": frames["times"]}


@register("odb.steps", "All Steps", "Data",
          "Every step of the ODB: names (connect to a Step setting, e.g. Field Output's, to loop over steps), "
          "how many frames each has, and their step times.",
          inputs=[Port("odb", "odb", "ODB")],
          outputs=[Port("names", "text", "Names"), Port("frames", "number", "Frames"), Port("count", "number", "Count")])
def odb_steps(inputs, params):
    steps = inputs["odb"].info["steps"]
    if not steps:
        raise ValueError("The ODB has no steps")
    return {"names": [Text(s["name"], s.get("description") or s["name"]) for s in steps],
            "frames": [Number(float(len(s["frames"])), f"frames in {s['name']}") for s in steps],
            "count": Number(float(len(steps)), "Steps")}


def _property_text(v) -> str:
    """A property's value as one line for the Properties table."""
    if isinstance(v, list):
        shown = ", ".join(_property_text(x) for x in v[:30])
        return shown + (f"… ({len(v)} in all)" if len(v) > 30 else "")
    if isinstance(v, (Text, Number, Bool, EntitySet, Field, Series, Odb)):
        return _as_text(v)
    return as_name(v) or str(v)


def _property_of(value, name: str):
    props = properties(value)
    if not props:
        raise ValueError(f"A {type(value).__name__} has no properties to read. Properties reads steps, parts, "
                         "instances, regions, conditions, interactions, jobs, ODBs, fields, sets, curves, "
                         "numbers and texts.")
    for key, v in props.items():
        if key.lower() == str(name).strip().lower():
            return v
    raise ValueError(f"No property '{name}' (available: {', '.join(props)})")


def _properties_display(outputs, inputs, params) -> dict | None:
    value = inputs.get("value")
    props = properties(value)
    if not props:
        return None
    chosen = str(params.get("property", "")).strip().lower()
    title = as_name(value) or getattr(value, "name", "") or getattr(value, "path", "") or type(value).__name__
    return {"kind": "info", "title": f"{title}: {params.get('property', '')}",
            "items": [[("▶ " if k.lower() == chosen else "") + k, _property_text(v)] for k, v in props.items()]}


@register("data.properties", "Properties", "Data",
          "Read a property of anything: a step's name, procedure, time period or previous step; a part's name, "
          "model or body; a job's ODB path; an ODB's name, steps, instances, sets, node/element counts; a field's "
          "step, frame, time or variable; a set's size (count) and labels; a curve's points; a text's length. Also "
          "called info, attribute, get, inspect, name of. Linked objects (a step's Model or Previous step) come out "
          "as themselves, so Properties nodes chain.",
          inputs=[Port("value", "any", "Value", depth=0,
                       help="Anything: a Step, Part, Instance, Region, Condition, Interaction, Job, ODB, Field, Set, "
                            "Curve, Number or Text. A list runs once per item.")],
          outputs=[Port("value", "any", "Property")],
          params=[Param("property", "select", "Name", "Property",
                        help="What to read; the list shows what the wired value has. Takes a wire: a property "
                             "name (a list of names reads each).")],
          hints=lambda i, p: {"property": [opt(k) for k in properties(i.get("value"))]} if i.get("value") is not None else {},
          display=_properties_display)
def data_properties(inputs, params):
    return {"value": _property_of(inputs["value"], params["property"])}


@register("list.range", "Number Range", "List",
          "Numbers from Start to Stop in steps, e.g. 0, 1, 2… Feeding a list into a node runs it once per item: "
          "that's how loops work here.",
          outputs=[Port("list", "number", "Numbers")],
          params=[Param("start", "number", 0.0, "Start"), Param("stop", "number", 10.0, "Stop"),
                  Param("step", "number", 1.0, "Step")], hidden=True)
def list_range(inputs, params):
    start, stop, step = float(params["start"]), float(params["stop"]), float(params["step"])
    if step == 0 or (stop - start) * step < 0:
        raise ValueError("Step must be non-zero and go from Start towards Stop")
    count = int(np.floor((stop - start) / step + 1e-9)) + 1
    if count > MAX_RANGE:
        raise ValueError(f"That's {count} numbers; the limit is {MAX_RANGE}")
    return {"list": [Number(start + i * step, f"#{i}") for i in range(count)]}


def _frame_value(f: dict) -> tuple[float, str]:
    """A frame's value and what it is: its time, a Frequency step's frequency, or a Buckling step's eigenvalue (the
    load factor, given in the frame's description: its value is the mode number)."""
    text = f.get("description") or ""
    eigen = re.search(r"Mode\s+(\d+):\s*EigenValue\s*=\s*([-+0-9.eEdD]+)", text)
    if eigen:
        return float(eigen.group(2).replace("D", "E").replace("d", "e")), f"load factor of mode {eigen.group(1)}"
    freq = re.search(r"Mode\s+(\d+):.*?Freq\s*=\s*([-+0-9.eEdD]+)", text)
    if freq:
        return float(freq.group(2).replace("D", "E").replace("d", "e")), f"frequency of mode {freq.group(1)}"
    return float(f["value"]), f"t at frame {f['index']}"


@register("odb.frames", "All Frames", "Data",
          "The frames of a step (all, or From–To every nth), as two lists: frame numbers and their times. Connect Frames to a Field Output's "
          "Frame input to read the field at every frame (a loop over frames).",
          inputs=[Port("odb", "odb", "ODB")],
          outputs=[Port("frames", "number", "Frames"), Port("times", "number", "Times")],
          params=[Param("step", "select", "", "Step", help=STEP_HELP), *FRAME_RANGE],
          hints=lambda i, p: _step_hints(i.get("odb"), p))
def odb_frames(inputs, params):
    frames = _frames_in(_step_or_last(inputs["odb"], params["step"]), params)
    return {"frames": [Number(float(f["index"]), f"frame {f['index']}", extra={"time": f["value"]}) for f in frames],
            "times": [Number(*_frame_value(f)) for f in frames]}


@register("list.to_series", "Numbers to Curve", "List",
          "Turn a list of numbers into a curve, e.g. one Reduce result per frame against the frame times.",
          inputs=[Port("y", "number", "Y"), Port("x", "number", "X", optional=True)],
          outputs=[Port("series", "series", "Curve")],
          params=[Param("name", "text", "", "Name"), Param("xlabel", "text", "", "X label")], lists=True, hidden=True)
def list_to_series(inputs, params):
    ys = _as_list(inputs["y"])
    xs = _as_list(inputs.get("x")) if inputs.get("x") is not None else None
    if xs is not None and len(xs) != len(ys):
        raise ValueError(f"X has {len(xs)} numbers but Y has {len(ys)}")
    x = np.array([n.value for n in xs]) if xs is not None else np.arange(len(ys), dtype=float)
    name = params["name"] or (ys[0].label if ys else "curve")
    return {"series": Series(name, x, np.array([n.value for n in ys], dtype=float),
                             params["xlabel"] or ("x" if xs is not None else "item"), name)}


def _curve_numbers(c: Series, axis: str) -> list[Number]:
    vals = c.x if axis == "x" else c.y
    return [Number(float(v), f"{c.name} {axis}[{i}]", f"{c.xlabel} = {_fmt(float(x))}", dict(c.extra))
            for i, (v, x) in enumerate(zip(vals, c.x))]


def _curves_apart(out) -> dict:
    """X and Y values as plain number lists: one list for one curve, one list per curve (a matrix) for several."""
    many = isinstance(out, list)
    xs = [_curve_numbers(c, "x") for c in _as_list(out)]
    ys = [_curve_numbers(c, "y") for c in _as_list(out)]
    return {"x": xs if many else xs[0], "y": ys if many else ys[0]}


@register("series.make", "Make Curve", "Math",
          "Build a curve from Y and X: two curves over time (e.g. force vs. displacement, matched by time), or two "
          "lists of numbers (e.g. one Reduce result per frame against the frame times). Without X, Y's own "
          "x (time) is kept, or the item number for a list. It also takes curves apart: X values and Y values "
          "give the times and values as plain number lists (one list per curve for several curves: a matrix, "
          "rows = curves).",
          inputs=[Port("y", "any", "Y", depth=1), Port("x", "any", "X", optional=True, depth=1)],
          outputs=[Port("series", "series", "Curve"), Port("x", "any", "X values"), Port("y", "any", "Y values")],
          params=[Param("name", "text", "", "Name"), Param("xlabel", "text", "", "X label"),
                  Param("xscale", "number", 1.0, "X factor"), Param("yscale", "number", 1.0, "Y factor")],
          lists=True)
def series_make(inputs, params):
    y, x = inputs["y"], inputs.get("x")
    xs, ys = float(params["xscale"]), float(params["yscale"])

    def curves(v):
        return isinstance(v, Series) or (isinstance(v, list) and v and all(isinstance(i, Series) for i in v))

    def numbers(v):
        return isinstance(v, (Number, Bool)) or (isinstance(v, list) and all(isinstance(i, (Number, Bool)) for i in v))

    def named(c: Series) -> Series:
        name = params["name"] or c.name
        return Series(name, c.x * xs, c.y * ys, params["xlabel"] or c.xlabel, c.ylabel, dict(c.extra))

    if curves(y):
        if x is None:
            out = [named(c) for c in _as_list(y)]
        elif isinstance(x, Series):
            out = [named(series_xy({"x": x, "y": c}, {"xscale": 1.0, "yscale": 1.0})["series"]) for c in _as_list(y)]
        else:
            raise ValueError("With curves in Y, X must be one curve (its values become the X axis)")
        result = out if isinstance(y, list) and len(out) > 1 else out[0]
        return {"series": result, **_curves_apart(result)}
    if numbers(y):
        if x is not None and not numbers(x):
            raise ValueError("With numbers in Y, X must be numbers too")
        c = list_to_series({"y": y, "x": x}, {"name": params["name"], "xlabel": params["xlabel"]})["series"]
        c = Series(c.name, c.x * xs, c.y * ys, c.xlabel, c.ylabel)
        return {"series": c, **_curves_apart(c)}
    raise ValueError("Y must be a curve, curves, or a list of numbers")


@register("input.integer", "Integer", "Input", "A whole number you type in, e.g. a frame number or a count.",
          outputs=[Port("value", "number", "Value")],
          params=[Param("value", "int", 0, "Value"), Param("label", "text", "", "Name")], hidden=True)
def input_integer(inputs, params):
    return {"value": Number(float(int(params["value"])), params["label"] or "Integer")}


@register("input.bool", "Yes/No", "Input", "A switch you set: Yes or No (e.g. to turn part of a graph on or off with If).",
          outputs=[Port("value", "bool", "Yes/No")],
          params=[Param("value", "select", "Yes", "Value", options=["Yes", "No"]), Param("label", "text", "", "Name")])
def input_bool(inputs, params):
    return {"value": Bool(params["value"] == "Yes", params["label"] or "Switch")}


# ------------------------------------------------------------------- loop
# A loop here is a list flowing through nodes: any node that gets a list where it expects one item
# runs once per item. These nodes make that explicit, like the loops of a script.
FOR_BY = ["Step", "Count"]


def _sequence(params) -> list[float]:
    """Start to Stop by Step, or Count values from Start to Stop spaced evenly (linspace) or by a constant ratio
    (geometric: each a fixed multiple of the last, for graded sizes)."""
    if str(params.get("by") or "Step") == "Step":
        return [n.value for n in list_range({}, params)["list"]]
    start, stop, count = float(params["start"]), float(params["stop"]), int(params.get("count") or 0)
    if count < 1:
        raise ValueError("Count must be at least 1")
    if count > MAX_RANGE:
        raise ValueError(f"That's {count} numbers; the limit is {MAX_RANGE}")
    if count == 1:
        return [start]
    if str(params.get("spacing") or "Linear") == "Geometric":
        if start == 0 or stop == 0 or (start > 0) != (stop > 0):
            raise ValueError("Geometric spacing needs Start and Stop of the same sign, neither 0")
        ratio = (stop / start) ** (1.0 / (count - 1))
        return [start * ratio ** k if k < count - 1 else stop for k in range(count)]
    return [start + (stop - start) * k / (count - 1) if k < count - 1 else stop for k in range(count)]


@register("loop.for", "For", "Loop",
          "for i = Start to Stop (inclusive) in steps of Step, or Count values from Start to Stop (evenly spaced, "
          "linspace; or geometric, a constant ratio for graded sizes). Makes a sequence / series / list of numbers. "
          "Connect i to any number input: that node, and everything after it, runs once per i.",
          outputs=[Port("i", "number", "i"), Port("count", "number", "Count")],
          params=[Param("by", "select", "Step", "By", options=FOR_BY,
                      help="Step: Start, Start + Step, … up to Stop. Count: that many values from Start to Stop."),
                  Param("start", "number", 1.0, "Start"), Param("stop", "number", 10.0, "Stop"),
                  Param("step", "number", 1.0, "Step", when={"by": ["Step"]}),
                  Param("count", "int", 10, "Count", when={"by": ["Count"]}),
                  Param("spacing", "select", "Linear", "Spacing", options=["Linear", "Geometric"],
                        when={"by": ["Count"]},
                        help="Linear: equal steps. Geometric: each value a constant multiple of the one before.")])
def loop_for(inputs, params):
    _finite(params, {"start": "Start", "stop": "Stop", "step": "Step"})
    values = _sequence(params)
    return {"i": [Number(round(v, 12), f"i = {_fmt(v)}") for v in values], "count": Number(float(len(values)), "Count")}


@register("loop.foreach", "For Each", "Loop",
          "Go through a list one item at a time: Item runs the next nodes once per item, Index says which one. "
          "A Set gives its nodes/elements (one-member sets), a Field its rows.",
          inputs=[Port("list", "any", "List", depth=1)],
          outputs=[Port("item", "any", "Item"), Port("index", "number", "Index"), Port("count", "number", "Count")],
          lists=True)
def loop_foreach(inputs, params):
    v = inputs["list"]
    if _is_collection(v):
        n = _size(v)
        if n > MAX_SPLIT:
            raise ValueError(f"That's {n} members; the limit is {MAX_SPLIT}. Use Sub-list first.")
        items = [_take(v, [i], f" {_member(v, i)}") for i in range(n)]
    else:
        items = _as_list(v)
    return {"item": items, "index": [Number(float(i), f"index {i}") for i in range(len(items))],
            "count": Number(float(len(items)), "Count")}


@register("loop.accumulate", "Accumulate", "Loop",
          "Carry a value through a loop: the running sum (or product, max, min, mean) after each item, and the "
          "final total.",
          inputs=[Port("values", "number", "Values")],
          outputs=[Port("running", "number", "Running"), Port("total", "number", "Total")],
          params=[Param("op", "select", "Sum", "Operation", options=["Sum", "Product", "Max", "Min", "Mean"])],
          lists=True)
def loop_accumulate(inputs, params):
    items: list[Number] = _as_list(inputs["values"])
    if not items:
        raise ValueError("Nothing to accumulate")
    v = np.array([n.value for n in items], dtype=float)
    op = params["op"]
    running = {"Sum": np.cumsum, "Product": np.cumprod, "Max": np.maximum.accumulate,
               "Min": np.minimum.accumulate, "Mean": lambda a: np.cumsum(a) / np.arange(1, len(a) + 1)}[op](v)
    return {"running": [Number(float(r), f"{op} after #{i}") for i, r in enumerate(running)],
            "total": Number(float(running[-1]), f"{op} of {len(items)}")}
