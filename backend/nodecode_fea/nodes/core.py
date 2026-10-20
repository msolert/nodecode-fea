"""The node library's building blocks: ports, settings, NodeDef and the register decorator.

``run(inputs, params)`` returns a dict of output values keyed by output port name.
``hints(inputs, params)`` (optional) returns dropdown options for parameters,
computed from whatever upstream data is already available.
``display(outputs, inputs, params)`` (optional) returns what the viewer panel shows;
nodes without one get a default preview of their first output."""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass
from typing import Any, Callable

from ..values import (AnalysisStep, Bool, Condition, DatumPlane, Field, Instance, Interaction, Job, Model, Number,
    Odb, OutputRequests, Part, PartOperation, Region, Sketch, Text)


MODEL_VALUES = (Model, Sketch, Part, Instance, Region, DatumPlane, PartOperation, AnalysisStep, Condition, Interaction, Job,
                OutputRequests)


@dataclass
class Port:
    name: str
    type: str
    label: str = ""
    optional: bool = False
    multi: bool = False
    help: str = ""
    # How deep a value it takes: 0 one item, 1 one list (Reduce, Count, Sort…), 2 a table; None (default) means 0
    # for typed inputs and "whatever comes" for 'any', multi and list-handling nodes (Formula, Merge, the outputs).
    # Deeper values run the node once per item, keeping the shape: a grid into Reduce gives one result per row.
    depth: int | None = None
    table: bool = False  # a row or matrix of numbers is one value here (points, profiles)
    when: dict | None = None  # shown only when settings have these values, e.g. {"shape": ["Profile"]} (or wired);
    # "unwired:<port>": only while that input port has no wire; "wired:<port>": only once it has one;
    # "named:<setting>": while that comma-separated setting names an item at one of these positions (More outputs)


@dataclass
class Param:
    name: str
    kind: str  # text | number | int | select | file | pick (a button that starts a pick in CAE) | action (buttons)
    default: Any
    label: str = ""
    options: list[Any] | None = None
    help: str = ""
    # Shown in the editor only when other settings have these values, e.g. {"shape": ["Circle"]}; always shown
    # when wired. It only tidies the node: the value still exists and is passed to the node.
    when: dict | None = None
    # Takes a value per analysis step, 'value; Step: value; ...' (stepwise.py); the editor's Per-step table edits it
    per_step: bool = False
    # Edited only through the Per-step table (Active in: its checkboxes); shown as a field only when wired
    table_only: bool = False


@dataclass
class NodeDef:
    type: str
    title: str
    category: str
    description: str
    inputs: list[Port]
    outputs: list[Port]
    params: list[Param]
    run: Callable[[dict, dict], dict]
    hints: Callable[[dict, dict], dict] | None = None
    display: Callable[[dict, dict, dict], dict] | None = None
    version: Callable[[dict, dict], Any] | None = None  # extra cache key from (params, inputs), e.g. ODB file mtime
    lists: bool = False  # True if run() handles list inputs itself instead of being mapped over items
    hidden: bool = False  # kept so saved graphs still run, but not offered in the palette (replaced by another node)
    replaced_by: str = ""  # for hidden nodes: the node that does the same now (searching the old name finds it)
    display_lists: bool = False  # display() also shows the outputs of a node looped over a list
    action: Callable[[dict, str], list[str]] | None = None  # what the node's buttons ("action" settings) do
    # after the whole graph ran: check(value, {model: [Initial, step names...]}) -> (steps, active steps) for the
    # editor's Per-step table, raising ValueError when its step names don't fit the model's steps
    late: Callable[[Any, dict], tuple] | None = None

    def to_json(self) -> dict:
        return {
            "type": self.type, "title": self.title, "category": self.category, "description": self.description,
            "inputs": [p.__dict__ | {"label": p.label or p.name} for p in self.inputs],
            "outputs": [p.__dict__ | {"label": p.label or p.name} for p in self.outputs],
            "params": [p.__dict__ | {"label": p.label or p.name, "port": param_port_type(p)} for p in self.params],
            "lists": self.lists,
            "hidden": self.hidden,
            "replacedBy": self.replaced_by,
        }


def param_port_type(p: Param) -> str | None:
    """Settings can also be fed by a wire: what kind of value their input port takes."""
    if p.kind in ("number", "int"):
        return "number"
    if p.kind in ("text", "file"):
        return "text"
    if p.kind == "select":
        return "number" if isinstance(p.default, (int, float)) and not isinstance(p.default, bool) else "text"
    return None


def param_from_value(p: Param, v) -> Any:
    """A wired value as the setting's own type (a Number into an Integer setting is rounded, and so on)."""
    want = param_port_type(p)
    label = p.label or p.name
    if isinstance(v, Bool):
        v = Number(1.0 if v.value else 0.0) if want == "number" else Text("Yes" if v.value else "No")
    if isinstance(v, Odb):
        v = Text(v.path)
    if want == "number":
        if isinstance(v, Text) and p.per_step and ":" in v.value:
            return v.value  # e.g. '0; Horizon: -9810' from Format Text
        if isinstance(v, Text):
            try:
                v = Number(float(v.value.strip()))
            except ValueError as exc:
                raise ValueError(f"Setting '{label}' needs a number, but got the text '{v.value}'") from exc
        if not isinstance(v, Number):
            raise ValueError(f"Setting '{label}' needs a number, not a {type(v).__name__}")
        return int(round(v.value)) if p.kind == "int" or isinstance(p.default, int) else float(v.value)
    if isinstance(v, Text):
        return v.value
    if isinstance(v, Number):
        return f"{v.value:.12g}" if p.kind != "select" else (str(int(v.value)) if float(v.value).is_integer() else str(v.value))
    raise ValueError(f"Setting '{label}' needs text, not a {type(v).__name__}")


REGISTRY: dict[str, NodeDef] = {}
CATEGORIES = ["Source", "Input", "Model", "Data", "Field", "Series", "Math", "Text", "Logic", "Loop", "List", "Output"]


def register(type: str, title: str, category: str, description: str, inputs=(), outputs=(), params=(),
             hints=None, display=None, version=None, lists=False, hidden=False, display_lists=False, action=None,
             late=None):
    def wrap(fn):
        REGISTRY[type] = NodeDef(type, title, category, description, list(inputs), list(outputs), list(params),
                                 fn, hints, display, version, lists, hidden, display_lists=display_lists,
                                 action=action, late=late)
        return fn
    return wrap


def opt(value, label=None):
    return {"value": value, "label": str(value) if label is None else label}


def _require_scalar(f: Field, what: str) -> np.ndarray:
    if f.values.shape[1] != 1:
        raise ValueError(f"{what} needs a scalar field, but '{f.name}' has components "
                         f"{', '.join(f.components)}. Add an Invariant or Component node first.")
    return f.values[:, 0]


STEP_HELP = "Last step unless chosen. Takes a wire: a step name, or a step number (1 = first step)."


def _step_or_last(odb: Odb, step: str) -> dict:
    if not odb.info["steps"]:
        raise ValueError("ODB has no steps")
    return odb.step(step) if step else odb.info["steps"][-1]


def _step_hints(odb: Odb | None, params: dict) -> dict:
    if odb is None:
        return {}
    steps = odb.info["steps"]
    out = {"step": [opt("", f"Last ({steps[-1]['name']})" if steps else "Last")] + [opt(s["name"]) for s in steps]}
    try:
        step = _step_or_last(odb, params.get("step", ""))
    except ValueError:
        return out
    frames = step["frames"]
    out["frame"] = [opt(-1, f"Last ({frames[-1]['value']:.4g})" if frames else "Last")] + [
        opt(f["index"], f"{f['index']}: t = {f['value']:.4g}") for f in frames]
    out["variable"] = [opt(f["name"], f"{f['name']} — {f['description']}") for f in step["fields"]]
    out["region"] = [opt(r["name"]) for r in step["historyRegions"]]
    for r in step["historyRegions"]:
        if r["name"] == params.get("region"):
            out["output"] = [opt(o["name"], f"{o['name']} — {o['description']}") for o in r["outputs"]]
    return out


FRAME_RANGE = [
    Param("first", "int", 0, "From frame", help="First frame to include (0 = first; -1 = last, -2 = one before…)"),
    Param("last", "int", -1, "To frame", help="Last frame to include (-1 = last). Takes a wire, like From."),
    Param("every", "int", 1, "Every nth", help="1 = every frame, 2 = every other one… The To frame is always kept."),
]


def _frames_in(step: dict, params: dict) -> list[dict]:
    """The step's frames from From to To (both included, negative counts from the end), every nth."""
    frames = step["frames"]
    n = len(frames)
    if not n:
        raise ValueError(f"Step '{step['name']}' has no frames")
    a, b = int(params.get("first", 0)), int(params.get("last", -1))
    a, b = a + n if a < 0 else a, b + n if b < 0 else b
    if not (0 <= a < n and 0 <= b < n):
        raise ValueError(f"Frames go from 0 to {n - 1} in step '{step['name']}' (or -1 for the last)")
    if a > b:
        raise ValueError(f"From frame ({a}) is after To frame ({b})")
    picked = frames[a:b + 1][::max(1, int(params.get("every", 1)))]
    if picked[-1] is not frames[b]:
        picked.append(frames[b])
    return picked


MAX_CURVES = 200  # Probe vs Time / Field vs Time "Each"


def _fmt(v: float) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:.4g}"
