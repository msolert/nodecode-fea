"""Evaluate a node graph: topological order, typed inputs, per-node errors, result caching."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import OrderedDict
from typing import Any

import numpy as np

from .nodes import REGISTRY, default_display, odb_from_path, param_from_value, param_port_type, summarize
from .values import PORT_TYPES, Number, Odb, Text
from .sources import NotReady, SourceError
from .stepwise import model_steps

CACHE_SIZE = 256


def jsonable(obj: Any) -> Any:
    """Convert numpy values (and NaN/inf) into something json.dumps accepts."""
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        if obj.dtype.kind == "f":
            return [None if not math.isfinite(v) else v for v in obj.tolist()]
        return obj.tolist()
    if isinstance(obj, np.generic):
        return jsonable(obj.item())
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _is_table(v) -> bool:
    """A row of numbers or a matrix (rows of numbers): one value for inputs that take tables (points, profiles)."""
    if not isinstance(v, list) or not v:
        return False
    if all(isinstance(x, Number) for x in v):
        return True
    return all(isinstance(r, list) and r and all(isinstance(x, Number) for x in r) for r in v)


def value_depth(v, table: bool = False) -> int:
    """How many list levels a value has: 0 for one item, 1 for a list, 2 for a list of lists (a grid)...
    For inputs that take tables, a matrix of numbers counts as one item."""
    if not isinstance(v, list) or (table and _is_table(v)):
        return 0
    first = next((x for x in v if x is not None), None)
    return 1 + (value_depth(first, table) if first is not None else 0)


def port_depth(defn, port) -> int | None:
    """How deep a value an input takes (0: one item, 1: one list, 2: a table); None takes whatever comes.
    Anything deeper than that runs the node once per item, at as many levels as needed."""
    if port.depth is not None:
        return port.depth
    if port.multi or port.type == "any" or defn.lists:
        return None
    return 0


def _leaves(v):
    if isinstance(v, list):
        for x in v:
            yield from _leaves(x)
    elif v is not None:
        yield v


def _first_item(v, levels: int):
    for _ in range(levels):
        v = next((x for x in v if x is not None), None) if isinstance(v, list) else v
    return v


def _shape(v) -> list[int]:
    """Sizes of the list levels: [10] for 10 items, [3, 4] for 3 lists of 4."""
    out = []
    while isinstance(v, list):
        out.append(len(v))
        v = next((x for x in v if x is not None), None)
    return out


def shown_value(out: dict, inputs: dict):
    """What the data panel shows for a node: its first output, or for outputs (Value…) its first input."""
    return next(iter(out.values()), None) if out else next(iter(inputs.values()), None)


def inspect_item(value, path: list[int]) -> dict:
    """The item at ``path`` (indices, one per list level) of a nested value, for the data panel: its display,
    summary, and the length of every list along the way (and of the item, if it is a list too)."""
    levels = []
    for depth, i in enumerate(path):
        if not isinstance(value, list):
            raise ValueError(f"Level {depth + 1} is not a list")
        levels.append(len(value))
        if not -len(value) <= i < len(value):
            raise ValueError(f"Item {i} is out of range (there are {len(value)})")
        value = value[i]
    if isinstance(value, list):
        levels.append(len(value))
    return {"display": jsonable(default_display(value)), "summary": summarize(value) if value is not None else "",
            "levels": levels}


class NodeInputs(dict):
    """Each node's input values (by node id), with its outputs in ``.outputs`` (the CAE export reads both)."""

    def __init__(self):
        super().__init__()
        self.outputs: dict[str, dict] = {}


class Engine:
    def __init__(self):
        self.cache: "OrderedDict[str, tuple[dict, dict | None, str]]" = OrderedDict()
        self.lock = threading.Lock()

    def run(self, graph: dict) -> dict[str, dict]:
        with self.lock:
            return self._run(graph, {})

    def evaluate(self, graph: dict) -> tuple[dict[str, dict], dict[str, dict]]:
        """Run the graph and also return the (Python) input values of every node that ran."""
        with self.lock:
            node_inputs = NodeInputs()
            return self._run(graph, node_inputs), node_inputs

    def _run(self, graph: dict, node_inputs: dict) -> dict[str, dict]:
        nodes = {n["id"]: n for n in graph.get("nodes", [])}
        edges = [e for e in graph.get("edges", []) if e.get("source") in nodes and e.get("target") in nodes]
        edges += _default_model_edges(graph, nodes, edges)
        incoming: dict[str, list[dict]] = {nid: [] for nid in nodes}
        for e in edges:
            incoming[e["target"]].append(_legacy_handle(e, nodes[e["target"]]))

        results: dict[str, dict] = {}
        outputs: dict[str, dict] = {}
        keys: dict[str, str] = {}

        for nid in self._order(nodes, edges, results):
            node = nodes[nid]
            res = results[nid] = {"status": "ok", "message": "", "summary": "", "display": None, "hints": {}}
            defn = REGISTRY.get(node.get("type"))
            if defn is None:
                res.update(status="error", message=f"Unknown node type '{node.get('type')}'")
                continue
            params = {p.name: p.default for p in defn.params}
            params.update({k: v for k, v in (node.get("params") or {}).items() if k in params})

            inputs: dict[str, Any] = {}
            input_keys: list[str] = []
            missing, blocked, off, waiting = [], [], [], []
            for port in defn.inputs:
                links = [e for e in incoming[nid] if (e.get("targetHandle") or port.name) == port.name]
                vals = []
                for e in links:
                    src = e["source"]
                    handle = e.get("sourceHandle") or "value"
                    if src in outputs and handle in outputs[src]:
                        vals.append(outputs[src][handle])
                        input_keys.append(f"{port.name}<{keys[src]}:{handle}")
                    elif results.get(src, {}).get("status") == "waiting" and src in nodes:
                        waiting.append(REGISTRY[nodes[src]["type"]].title)  # e.g. results of a job still to run
                    elif results.get(src, {}).get("status") == "disabled":
                        # A deactivated node that passes nothing on: as if unwired (optional inputs simply lose it)
                        input_keys.append(f"{port.name}<off:{src}")
                        if not port.optional and not port.multi:
                            off.append(REGISTRY[nodes[src]["type"]].title)
                    else:
                        blocked.append(port.label or port.name)
                if port.multi:
                    # Several wires and/or lists into one port: flatten into a single list.
                    inputs[port.name] = [x for v in vals for x in (v if isinstance(v, list) else [v])]
                    if not vals and not port.optional and not blocked:
                        missing.append(port.label or port.name)
                else:
                    inputs[port.name] = vals[0] if vals else None
                    if not vals and not port.optional:
                        missing.append(port.label or port.name)

            # the graph's Model, for a Sketch / Part / Step whose own isn't wired (see _default_model_edges)
            for e in incoming[nid]:
                if e.get("targetHandle") == DEFAULT_MODEL_HANDLE and e["source"] in outputs:
                    inputs[DEFAULT_MODEL_HANDLE] = outputs[e["source"]].get(e.get("sourceHandle") or "model")
                    input_keys.append(f"{DEFAULT_MODEL_HANDLE}<{keys[e['source']]}")

            if node.get("disabled"):
                # Deactivated: its main (first) output passes on an input of the same type, so a chain keeps running
                # without this step (a filter keeps everything, a conversion is skipped); otherwise it gives nothing.
                out = {}
                for op in defn.outputs[:1]:
                    src_port = next((ip for ip in defn.inputs if ip.type == op.type and inputs.get(ip.name) is not None
                                     and inputs.get(ip.name) != []), None)
                    if src_port is not None:
                        out[op.name] = inputs[src_port.name]
                keys[nid] = hashlib.sha1(json.dumps(["off", sorted(input_keys)]).encode()).hexdigest()
                outputs[nid] = out
                first = next(iter(out.values()), None)
                res.update(status="disabled", summary=summarize(first) if first is not None else "",
                           message="Deactivated" + (": passes its input through" if out else ""))
                continue

            # An ODB input also takes a file path (Text), and a text input an ODB (its path): no Open ODB needed.
            try:
                for port in defn.inputs:
                    if port.type in ("odb", "text") and inputs.get(port.name) is not None:
                        inputs[port.name] = _coerce(inputs[port.name], port.type, input_keys)
            except (SourceError, ValueError) as exc:
                res.update(status="waiting" if isinstance(exc, NotReady) else "error",
                           message=f"{port.label or port.name}: {exc}")
                continue

            # Settings fed by a wire (target handle "param:<name>") override the typed-in value.
            param_defs = {p.name: p for p in defn.params if param_port_type(p)}
            wired: dict[str, Any] = {}
            for e in incoming[nid]:
                handle_in = e.get("targetHandle") or ""
                if not handle_in.startswith("param:") or handle_in[6:] not in param_defs:
                    continue
                name, src, handle = handle_in[6:], e["source"], e.get("sourceHandle") or "value"
                if src in outputs and handle in outputs[src]:
                    wired[name] = outputs[src][handle]
                    input_keys.append(f"param:{name}<{keys[src]}:{handle}")
                elif results.get(src, {}).get("status") == "disabled":
                    input_keys.append(f"param:{name}<off:{src}")  # the typed-in value applies again
                else:
                    blocked.append(param_defs[name].label or name)
            param_lists = {k: v for k, v in wired.items() if isinstance(v, list)}
            try:
                for k, v in wired.items():
                    if not isinstance(v, list):
                        params[k] = param_from_value(param_defs[k], v)
                hint_params = {**params, **{k: param_from_value(param_defs[k], _first_item(v, value_depth(v)))
                                             for k, v in param_lists.items() if v}}
            except ValueError as exc:
                res.update(status="error", message=str(exc))
                continue

            # Inputs deeper than the node takes (a list into a one-item input, a grid into a list input...) run it
            # once per item, at every extra level; the outputs keep the same nesting. A node set to take its inputs
            # whole ("levels": "whole") never loops over them: a grid into Pick from List picks rows.
            whole = node.get("levels") == "whole"
            depths = {p.name: None if whole else port_depth(defn, p) for p in defn.inputs}
            extra = {p.name: value_depth(inputs.get(p.name), p.table) - depths[p.name] for p in defn.inputs
                     if depths[p.name] is not None and inputs.get(p.name) is not None}
            extra = {k: e for k, e in extra.items() if e > 0}

            if defn.hints:
                try:
                    first = {k: (_first_item(v, extra[k]) if k in extra else v) for k, v in inputs.items()}
                    res["hints"] = defn.hints(first, hint_params)
                except Exception:  # hints are best-effort
                    res["hints"] = {}

            if blocked:
                res.update(status="blocked", message="Waiting on an upstream node with an error")
                continue
            if waiting:
                res.update(status="waiting", message=f"Waiting for {', '.join(dict.fromkeys(waiting))}")
                continue
            if off:
                res.update(status="blocked", message=f"Waiting: {', '.join(dict.fromkeys(off))} is deactivated")
                continue
            if missing:
                res.update(status="waiting", message=f"Connect {', '.join(missing)}")
                continue

            problem = self._type_problem(defn, inputs)
            if problem:
                res.update(status="error", message=problem)
                continue

            combine = "all" if node.get("combine") == "all" else "pairs"
            version = defn.version(params, inputs) if defn.version else None
            key = hashlib.sha1(json.dumps([defn.type, params, sorted(input_keys), version, combine, whole],
                                          sort_keys=True, default=str).encode()).hexdigest()
            keys[nid] = key
            try:
                if key in self.cache:
                    self.cache.move_to_end(key)
                    out, display, summary = self.cache[key]
                else:
                    looped = bool(extra or param_lists)
                    if looped:
                        if defn.category == "Output":
                            raise ValueError(f"{defn.title} shows a single item; use List Item to pick one")
                        lists = {("in", k): (inputs[k], e) for k, e in extra.items()}
                        lists |= {("param", k): (v, value_depth(v)) for k, v in param_lists.items()}
                        out = self._run_looped(defn, inputs, params, lists,
                                               {k: param_defs[k] for k in param_lists}, combine,
                                               set(param_lists) | set(extra), ())
                    else:
                        out = defn.run(inputs, params)
                    first = next(iter(out.values()), None)
                    display = (defn.display(out, inputs, params) if defn.display and (not looped or defn.display_lists)
                               else default_display(first))
                    summary = summarize(first) if first is not None else ""
                    if looped and first is not None and len(_shape(first)) > 1:
                        summary = " × ".join(map(str, _shape(first))) + " items"
                    if defn.display and not summary and display and display.get("kind") == "value":
                        summary = f"{display['value']:.4g}"
                    self.cache[key] = (out, display, summary)
                    while len(self.cache) > CACHE_SIZE:
                        self.cache.popitem(last=False)
            except Exception as exc:  # one bad node must not take down the whole graph
                if isinstance(exc, NotReady):
                    res.update(status="waiting", message=str(exc))
                    continue
                if isinstance(exc, (SourceError, ValueError)):
                    msg = str(exc)
                elif isinstance(exc, KeyError):
                    msg = f"Not found: {exc}"
                else:
                    msg = f"{type(exc).__name__}: {exc}"
                res.update(status="error", message=msg)
                continue
            outputs[nid] = out
            node_inputs[nid] = inputs
            if isinstance(node_inputs, NodeInputs):
                node_inputs.outputs[nid] = out
            res.update(summary=summary, display=jsonable(display))
            v = shown_value(out, inputs)
            if isinstance(v, list) and v:  # the data panel can open its items, level by level
                res["shape"] = _shape(v)
        self._late(nodes, outputs, results)
        return results

    @staticmethod
    def _late(nodes: dict, outputs: dict, results: dict) -> None:
        """Checks that need the whole graph: a load's 'Step: value' names against every Step of its model (the
        later ones aren't upstream of it). Gives the editor the steps (hints __steps / __active)."""
        late = [(nid, REGISTRY[nodes[nid]["type"]]) for nid in outputs
                if results.get(nid, {}).get("status") == "ok" and REGISTRY[nodes[nid]["type"]].late]
        if not late:
            return
        steps = model_steps(v for out in outputs.values() for v in out.values())
        for nid, defn in late:
            first = next(iter(outputs[nid].values()), None)
            try:
                names, active, cells, error = defn.late(first, steps)
            except Exception:  # best-effort, like hints
                continue
            results[nid].setdefault("hints", {}).update(
                __steps=[{"value": n, "label": n} for n in names], __active=[{"value": n, "label": n} for n in active],
                __cells=[{"value": cells, "label": ""}])
            if error:  # the table stays (its hints above) so the steps can be put right
                results[nid].update(status="error", message=error)

    @staticmethod
    def _type_problem(defn, inputs: dict) -> str:
        """Wires typed 'any' (from list nodes) can carry the wrong kind of value; say so clearly."""
        for port in defn.inputs:
            if port.type == "any":
                continue
            for item in _leaves(inputs.get(port.name)):
                if not isinstance(item, PORT_TYPES[port.type]):
                    got = next((k for k, cls in PORT_TYPES.items() if isinstance(item, cls)), type(item).__name__)
                    return f"'{port.label or port.name}' expects {port.type} values but got {got}"
        return ""

    @classmethod
    def _run_looped(cls, defn, inputs: dict, params: dict, lists: dict, param_defs: dict, combine: str,
                    varied: set, path: tuple) -> dict:
        """Run the node once per item of the inputs (and list-fed settings) that are deeper than it takes.

        lists: {("in" | "param", name): (value, extra levels)}. With combine "pairs" the deepest ones are walked
        together, item by item (a list of one goes with every item); with "all" one is walked at a time, so two
        lists give every combination (a grid). Outputs keep the nesting. Each run gets params["__path__"] (its
        indices) and params["__varied__"] (the inputs and settings fed by lists), so nodes can name what they make
        uniquely."""
        todo = {k: v for k, v in lists.items() if v[1] > 0}
        if not todo:
            run_params = dict(params)
            for (kind, k), (v, _) in lists.items():
                if kind == "param":
                    run_params[k] = param_from_value(param_defs[k], v)
            run_inputs = {k: lists[("in", k)][0] if ("in", k) in lists else v for k, v in inputs.items()}
            run_params["__path__"], run_params["__varied__"] = path, varied
            return defn.run(run_inputs, run_params)
        if combine == "all":
            walk = [next(iter(todo))]
        else:
            top = max(e for _, e in todo.values())
            walk = [k for k, (_, e) in todo.items() if e == top]
        lengths = {len(lists[k][0]) for k in walk}
        n = max(lengths)
        if lengths - {1, n}:
            raise ValueError(f"Lists have different lengths ({', '.join(map(str, sorted(lengths)))}); use lists of "
                             "the same length, or a single item for every item, or set the node to All combinations")
        if n == 0:
            raise ValueError("The input list is empty")
        outs: dict[str, list] = {}
        for i in range(n):
            sub = dict(lists)
            for k in walk:
                v, e = lists[k]
                sub[k] = (v[i if len(v) > 1 else 0], e - 1)
            for port, value in cls._run_looped(defn, inputs, params, sub, param_defs, combine, varied,
                                               path + (i,)).items():
                outs.setdefault(port, []).append(value)
        return outs

    @staticmethod
    def _order(nodes: dict, edges: list[dict], results: dict) -> list[str]:
        indeg = {nid: 0 for nid in nodes}
        children: dict[str, list[str]] = {nid: [] for nid in nodes}
        for e in edges:
            indeg[e["target"]] += 1
            children[e["source"]].append(e["target"])
        ready = [nid for nid, d in indeg.items() if d == 0]
        order = []
        while ready:
            nid = ready.pop(0)
            order.append(nid)
            for c in children[nid]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
        for nid in nodes:
            if nid not in order:
                results[nid] = {"status": "error", "message": "Part of a cycle", "summary": "",
                                "display": None, "hints": {}}
        return order


DEFAULT_MODEL_HANDLE = "__default_model"
TAKES_A_MODEL = {"model.sketch": "model", "model.part": "model", "model.step": "after"}


def _default_model_edges(graph: dict, nodes: dict, edges: list[dict]) -> list[dict]:
    """A Sketch, Part or Step whose Model isn't wired is built in the graph's Model, not NoCode-Model: the one
    graph["model"] names (the Model node selected last in the editor), else the first; with no Model node, the
    model of a sketch, part or instance chosen from CAE (an Input), so work on an existing model stays in it. The
    node gets it as inputs["__default_model"] and uses it only after its own Model (and a Part, its sketch's)."""
    models = [n for n in nodes.values() if n.get("type") == "model.model" and not n.get("disabled")]
    handle = "model"
    if not models:
        models = [n for n in nodes.values() if n.get("type") == "input.value" and not n.get("disabled")
                  and str((n.get("params") or {}).get("type", "")).endswith(" in CAE")]
        handle = "value"
    if not models:
        return []
    chosen = next((n for n in models if n["id"] == graph.get("model")), models[0])
    wired = {(e["target"], e.get("targetHandle")) for e in edges}
    return [{"source": chosen["id"], "sourceHandle": handle, "target": nid, "targetHandle": DEFAULT_MODEL_HANDLE}
            for nid, n in nodes.items()
            if n.get("type") in TAKES_A_MODEL and (nid, TAKES_A_MODEL[n["type"]]) not in wired]


def _legacy_handle(edge: dict, node: dict) -> dict:
    """A wire into an input a node no longer has, but which is now a setting (e.g. Field Output's old 'Frame #'
    port, now the Frame setting), goes to that setting, so graphs saved before the change keep working."""
    defn = REGISTRY.get(node.get("type"))
    handle = edge.get("targetHandle") or ""
    if (defn is None or not handle or handle.startswith("param:") or any(p.name == handle for p in defn.inputs)
            or not any(p.name == handle and param_port_type(p) for p in defn.params)):
        return edge
    return {**edge, "targetHandle": f"param:{handle}"}


def _coerce(value, port_type: str, input_keys: list[str]):
    """Text paths into ODB inputs become opened ODBs (the file's version joins the cache key); ODBs into text inputs
    become their path."""
    if isinstance(value, list):
        return [_coerce(v, port_type, input_keys) for v in value]
    if port_type == "odb" and isinstance(value, Text):
        odb = odb_from_path(value.value)
        input_keys.append(f"odb-file:{odb.path}@{odb.version}")
        return odb
    if port_type == "text" and isinstance(value, Odb):
        return Text(value.path, "ODB path")
    return value
