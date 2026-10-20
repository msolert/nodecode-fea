"""Nodes merged into more general ones: still run in saved graphs; searching their names finds the new node."""

from __future__ import annotations

from .core import REGISTRY


# Nodes merged into more general ones: still run in saved graphs; searching their names finds the new node.
REPLACED_BY = {
    "math.expression": "math.formula", "field.expression": "math.formula", "series.expression": "math.formula",
    "text.to_number": "math.formula",
    "logic.compare": "logic.test", "field.where": "logic.test", "text.test": "logic.test",
    "field.invariant": "field.quantity", "field.component": "field.quantity",
    "field.reduce": "math.reduce", "series.reduce": "math.reduce", "list.stats": "math.reduce",
    "field.probe": "field.time", "field.over_frames": "field.time",
    "list.item": "list.pick", "list.slice": "list.pick", "list.reverse": "list.pick",
    "list.split": "loop.foreach", "list.range": "loop.for",
    "series.xy": "series.make", "list.to_series": "series.make",
    "input.integer": "input.value", "text.from": "text.format", "text.join": "text.format",
    "list.filter": "logic.if", "logic.gate": "math.formula", "loop.accumulate": "math.reduce",
    "cae.pick": "odb.set", "view.table": "view.value", "source.files": "odb.open",
    "math.number": "input.value", "input.bool": "input.value", "text.value": "input.value",
    "odb.steps": "odb.timeline", "odb.frames": "odb.timeline", "field.filter": "set.op",
}
for _old, _new in REPLACED_BY.items():
    REGISTRY[_old].replaced_by = _new
    REGISTRY[_old].hidden = True
