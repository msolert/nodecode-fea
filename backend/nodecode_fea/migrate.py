"""Turn nodes that were merged into general ones into those general nodes, in saved graphs.

Old node types still run (they stay registered, hidden), but a graph that shows them is confusing next to a palette
that doesn't. ``migrate_graph`` rewrites each old node as its replacement: new type, settings translated, and the
wires moved to the matching ports. Every entry of ``nodes.REPLACED_BY`` must have a rule here (a test checks).
"""

from __future__ import annotations

import ast
import copy
from typing import Callable

from .nodes import EACH, REGISTRY, REPLACED_BY


def _rename(expr: str, names: dict[str, str]) -> str:
    """Rename variables in a formula, e.g. Field Math's x -> a and a -> b."""
    try:
        tree = ast.parse(expr.strip() or "0", mode="eval")
    except SyntaxError:
        return expr
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in names:
            node.id = names[node.id]
    return ast.unparse(tree)


def _num_text(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else repr(f)


# Each rule: (new type, input handle map, output handle map, params(old params, connected input handles) -> new).
# Handles not in a map keep their name. A rule may also be a function of the connected inputs returning such a tuple.
Rule = tuple[str, dict, dict, Callable[[dict, set], dict]]

GATE = {"And": "a and b", "Or": "a or b", "Xor": "a != b", "Not a": "not a"}
TEXT_OPS = {"equals": "=="}


def _list_filter(p: dict, connected: set) -> Rule:
    if "keep" in connected:  # keep where a Yes/No list says so: If
        return ("logic.if", {"list": "then", "keep": "condition"}, {"list": "result"}, lambda p, c: {})
    return ("logic.test", {"list": "a"}, {"list": "kept"},
            lambda p, c: {"op": p.get("op", ">"), "value": _num_text(p.get("value", 0))})


RULES: dict[str, Rule | Callable[[dict, set], Rule]] = {
    "math.expression": ("math.formula", {}, {}, lambda p, c: {"expr": p.get("expr", "a"), "label": p.get("label", "")}),
    "field.expression": ("math.formula", {"field": "a", "a": "b"}, {"field": "value"},
                         lambda p, c: {"expr": _rename(p.get("expr", "x"), {"x": "a", "a": "b"}), "label": ""}),
    "series.expression": ("math.formula", {"series": "a", "a": "b"}, {"series": "value"},
                          lambda p, c: {"expr": _rename(p.get("expr", "y"), {"y": "a", "a": "b"}), "label": ""}),
    "text.to_number": ("math.formula", {"text": "a"}, {}, lambda p, c: {"expr": "a", "label": ""}),
    "logic.gate": ("math.formula", {}, {"result": "value"},
                   lambda p, c: {"expr": GATE.get(p.get("op", "And"), "a and b"), "label": ""}),
    "logic.compare": ("logic.test", {}, {}, lambda p, c: {"op": p.get("op", ">"),
                                                          "value": _num_text(p.get("value", 0))}),
    "field.where": ("logic.test", {"field": "a"}, {"field": "kept"},
                    lambda p, c: {"op": p.get("op", ">"), "value": _num_text(p.get("value", 0))}),
    "text.test": ("logic.test", {"text": "a", "other": "b"}, {},
                  lambda p, c: {"op": TEXT_OPS.get(p.get("op", "contains"), p.get("op", "contains")),
                                "value": str(p.get("value", "")), "case": p.get("case", "Ignore case")}),
    "list.filter": _list_filter,
    "field.invariant": ("field.quantity", {"param:invariant": "param:quantity"}, {}, lambda p, c: {"quantity": p.get("invariant", "Mises")}),
    "field.component": ("field.quantity", {"param:component": "param:quantity"}, {}, lambda p, c: {"quantity": p.get("component", "")}),
    "field.reduce": ("math.reduce", {}, {}, lambda p, c: {"op": p.get("op", "Max")}),
    "series.reduce": ("math.reduce", {"series": "field"}, {},
                      lambda p, c: {"op": {"Final": "Last", "Initial": "First"}.get(p.get("op", "Max"),
                                                                                    p.get("op", "Max"))}),
    "list.stats": ("math.reduce", {"list": "field"}, {}, lambda p, c: {"op": p.get("op", "Max")}),
    "loop.accumulate": ("math.reduce", {"values": "field"}, {"total": "value"}, lambda p, c: {"op": p.get("op", "Sum")}),
    "field.probe": ("field.time", {}, {}, lambda p, c: {**p, "op": EACH}),
    "field.over_frames": ("field.time", {}, {},
                          lambda p, c: {**p, "op": EACH if str(p.get("op", "Max")).startswith("Each")
                                        else p.get("op", "Max")}),
    "list.item": ("list.pick", {"param:index": "param:which"}, {"item": "list"}, lambda p, c: {"which": _num_text(p.get("index", 0))}),
    "list.slice": ("list.pick", {"param:range": "param:which"}, {}, lambda p, c: {"which": str(p.get("range", "0:3"))}),
    "list.reverse": ("list.pick", {}, {}, lambda p, c: {"which": "::-1"}),
    "list.split": ("loop.foreach", {}, {"list": "item"}, lambda p, c: {}),
    "list.range": ("loop.for", {}, {"list": "i"}, lambda p, c: dict(p)),
    "series.xy": ("series.make", {}, {}, lambda p, c: dict(p)),
    "list.to_series": ("series.make", {}, {}, lambda p, c: dict(p)),
    "input.integer": ("input.value", {}, {},
                      lambda p, c: {"type": "Number", "value": _num_text(p.get("value", 0)), "label": p.get("label", "")}),
    "math.number": ("input.value", {}, {},
                    lambda p, c: {"type": "Number", "value": _num_text(p.get("value", 355)),
                                  "label": p.get("label", "")}),
    "input.bool": ("input.value", {}, {},
                   lambda p, c: {"type": "Yes/No", "value": p.get("value", "Yes"), "label": p.get("label", "")}),
    "text.value": ("input.value", {"param:text": "param:value"}, {"text": "value"},
                   lambda p, c: {"type": "Text", "value": str(p.get("text", "")), "label": ""}),
    "text.from": ("text.format", {"value": "a"}, {},
                  lambda p, c: {"template": "{a:.%dg}" % max(1, int(p.get("digits", 4)))}),
    "text.join": ("text.format", {}, {},
                  lambda p, c: {"template": str(p.get("sep", "")).replace("{", "{{").replace("}", "}}").join(
                      "{%s}" % k for k in "abcd" if k in c) or "{a}"}),
    "cae.pick": ("odb.set", {}, {}, lambda p, c: {"name": "", "kind": p.get("kind", "node"),
                                                  "labels": p.get("labels", "")}),
    "view.table": ("view.value", {"field": "value"}, {},
                   lambda p, c: {"sort": p.get("sort", "Descending"), "limit": p.get("limit", 50)}),
    "source.files": ("odb.open", {}, {"paths": "odb"},
                     lambda p, c: {"path": _folder_pattern(p)}),
    "odb.steps": ("odb.timeline", {}, {"names": "steps", "frames": "nframes"}, lambda p, c: {}),
    "odb.frames": ("odb.timeline", {}, {}, lambda p, c: dict(p)),
    "field.filter": ("set.op", {"field": "a", "set": "b"}, {"field": "result"}, lambda p, c: {"op": "Field on set"}),
}


def _folder_pattern(p: dict) -> str:
    folder = str(p.get("folder", "")).strip().rstrip("/\\")
    pattern = str(p.get("pattern", "*.odb")).strip() or "*.odb"
    sep = "\\" if "\\" in folder else "/"
    return folder + sep + ("**" + sep if p.get("subfolders") == "Yes" else "") + pattern


def migrate_graph(graph: dict) -> tuple[dict, list[str]]:
    """The graph with every replaced node turned into its general replacement, and what was changed."""
    g = copy.deepcopy(graph)
    nodes, edges = g.get("nodes", []), g.get("edges", [])
    notes: list[str] = []
    for node in nodes:
        data = node.get("data") if isinstance(node.get("data"), dict) else node  # editor nodes keep it in data
        old = data.get("type")
        rule = RULES.get(old)
        if rule is None:
            continue
        connected = {e.get("targetHandle") for e in edges if e.get("target") == node["id"]}
        if callable(rule):
            rule = rule(data.get("params") or {}, connected)
        new_type, in_map, out_map, params_fn = rule
        old_params = {p.name: p.default for p in REGISTRY[old].params} | (data.get("params") or {})
        new_def = REGISTRY[new_type]
        allowed = {p.name for p in new_def.params}
        data["params"] = {k: v for k, v in params_fn(old_params, connected).items() if k in allowed}
        data["type"] = new_type
        for e in edges:
            if e.get("target") == node["id"] and e.get("targetHandle") in in_map:
                e["targetHandle"] = in_map[e["targetHandle"]]
            if e.get("source") == node["id"] and e.get("sourceHandle") in out_map:
                e["sourceHandle"] = out_map[e["sourceHandle"]]
        notes.append(f"{REGISTRY[old].title} → {new_def.title}")
    # Wires to ports the new node doesn't have (e.g. All Steps' Count) are dropped, so nothing dangles.
    by_id = {n["id"]: (n.get("data") if isinstance(n.get("data"), dict) else n) for n in nodes}

    def has_port(node_id: str, handle: str | None, out: bool) -> bool:
        d = REGISTRY.get(by_id.get(node_id, {}).get("type", ""))
        if d is None or handle is None:
            return True
        if out:
            return any(p.name == handle for p in d.outputs)
        if handle.startswith("param:"):
            return any(p.name == handle[6:] for p in d.params)
        return any(p.name == handle for p in d.inputs)

    kept = [e for e in edges if has_port(e.get("source"), e.get("sourceHandle"), True)
            and has_port(e.get("target"), e.get("targetHandle"), False)]
    if len(kept) != len(edges):
        notes.append(f"{len(edges) - len(kept)} wire(s) to ports that no longer exist were removed")
    for e in kept:  # editor edge ids encode their handles
        if "id" in e and e.get("sourceHandle") is not None:
            e["id"] = f"{e['source']}.{e['sourceHandle']}-{e['target']}.{e['targetHandle']}"
    g["edges"] = kept
    return g, notes


def check_rules() -> list[str]:
    """Replaced node types without a migration rule (should be none)."""
    return [t for t in REPLACED_BY if t not in RULES]
