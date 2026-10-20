"""Default previews: what the viewer shows for a node without its own display."""

from __future__ import annotations

import os
import numpy as np
from typing import Any

from .. import jobs
from ..values import Amplitude, Bool, EntitySet, Field, Number, Odb, Part, Series, Sketch, Text
from .core import _fmt, MODEL_VALUES
from .common import ASSEMBLY, entity_name, _is_matrix, _numbers_table
from .lists import _flatten_all
from .output import _chart_display, _table_display
from .model.geometry import _model_value_display


def _bools_table(items: list, title: str) -> dict:
    rows = [[i, b.label, "Yes" if b.value else "No"] for i, b in enumerate(items)]
    yes = sum(1 for b in items if b.value)
    return {"kind": "table", "title": title, "columns": ["#", "Test", "Result"], "rows": rows, "allRows": rows,
            "total": len(rows), "stats": [{"name": "Yes", "min": yes, "max": yes}]}


def _matrix_table(m: list) -> dict:
    ncol = max(len(r) for r in m)
    rows = [[i, *[x.value if isinstance(x, Text) else float(x.value) for x in r]] for i, r in enumerate(m)]
    return {"kind": "table", "title": f"{len(m)}×{ncol} matrix", "columns": ["row", *[str(j) for j in range(ncol)]],
            "rows": rows, "allRows": rows, "total": len(rows), "stats": []}


def _model_list_display(value: list) -> dict | None:
    """A (nested) list of sketches / parts / instances: profiles drawn together, else a list of what each is."""
    items = _flatten_all(value)
    if not items or not all(isinstance(x, MODEL_VALUES) for x in items):
        return None
    sketches = [x if isinstance(x, Sketch) else x.sketch for x in items if isinstance(x, (Sketch, Part))]
    what = f"{len(items)} {type(items[0]).__name__.lower()}{'es' if isinstance(items[0], Sketch) else 's'}"
    if len(sketches) == len(items) and all(sk is not None for sk in sketches):
        return {"kind": "sketch", "title": what,
                "lines": [[[float(x), float(y)] for x, y in line] for sk in sketches for line in sk.outline()]}
    return {"kind": "info", "title": what, "items": [[getattr(x, "name", ""), x.summary()] for x in items[:200]]}


def default_display(value: Any) -> dict | None:
    if isinstance(value, MODEL_VALUES):
        return _model_value_display(value)
    if isinstance(value, list) and value and isinstance(next(iter(_flatten_all(value)), None), MODEL_VALUES):
        return _model_list_display(value)
    if _is_matrix(value):
        return _matrix_table(value)
    if isinstance(value, Odb):
        info = value.info
        steps = info["steps"]
        running = [] if value.path.startswith("mock://") or not jobs.live(value.path) else [[
            "Job", "Running: results so far, updating with each increment" + (
                f" ({p})" if (p := jobs.progress(jobs.read_text(os.path.splitext(value.path)[0] + ".sta")))
                else "")]]
        return {"kind": "info", "title": value.path, "items": [
            *running,
            ["Steps", ", ".join(f"{s['name']} ({len(s['frames'])} frames)" for s in steps)],
            ["Instances", ", ".join(f"{i['name']} ({i['nodes']} nodes, {i['elements']} elements)"
                                    for i in info["instances"])],
            ["Field outputs", ", ".join(f["name"] for f in steps[-1]["fields"]) if steps else ""],
            ["Sets", ", ".join(s["name"] for s in info["sets"])],
        ]}
    if isinstance(value, Field):
        return _table_display(value)
    if isinstance(value, EntitySet):
        return {"kind": "info", "title": value.name, "items": [
            ["Type", f"{value.kind} set"], ["Members", str(len(value.labels))],
            ["Instances", ", ".join(sorted({str(i) or ASSEMBLY for i in value.instances}))],
            ["Labels (in order)", ", ".join(f"[{k}] {entity_name(str(i), l)}"
                                            for k, (i, l) in enumerate(zip(value.instances[:60], value.labels[:60])))
             + (" …" if len(value.labels) > 60 else "")]]}
    if isinstance(value, Series):
        return _chart_display([value])
    if isinstance(value, list) and value and all(isinstance(v, Series) for v in value):
        return _chart_display(value)
    if isinstance(value, list) and value and all(isinstance(v, Number) for v in value):
        return _numbers_table(value, f"{len(value)} numbers")
    if isinstance(value, list) and value and all(isinstance(v, Bool) for v in value):
        return _bools_table(value, f"{len(value)} Yes/No values")
    if isinstance(value, Bool):
        return {"kind": "text", "text": "Yes" if value.value else "No", "label": value.label}
    if isinstance(value, Text):
        return {"kind": "text", "text": value.value, "label": value.label}
    if isinstance(value, list) and value and all(isinstance(v, Text) for v in value):
        rows = [[i, v.label, v.value] for i, v in enumerate(value)]
        return {"kind": "table", "title": f"{len(value)} texts", "columns": ["#", "Name", "Text"], "rows": rows,
                "allRows": rows, "total": len(rows), "stats": []}
    if isinstance(value, list):
        names = {Odb: "ODB", Field: "Field", EntitySet: "Set", Series: "Curve", Number: "Number", Bool: "Yes/No",
                 Text: "Text"}
        return {"kind": "info", "title": f"List of {len(value)} items", "items": [
            [f"#{i}", f"{names.get(type(v), type(v).__name__)} · {summarize(v)}"] for i, v in enumerate(value[:200])]}
    if isinstance(value, Number):
        return {"kind": "value", "value": value.value, "label": value.label, "unit": "", "where": value.where,
                "digits": 4}
    return None


def summarize(value: Any) -> str:
    if isinstance(value, MODEL_VALUES + (Amplitude,)):
        return value.summary()
    if _is_matrix(value):
        return f"{len(value)}×{max(len(r) for r in value)} matrix"
    if isinstance(value, Odb):
        steps = value.info["steps"]
        n_el = sum(i["elements"] for i in value.info["instances"])
        return f"{len(steps)} step{'s' * (len(steps) != 1)} · {n_el} elements"
    if isinstance(value, Field):
        if value.values.shape[1] == 1 and len(value.labels):
            return f"{len(value.labels)} {value.entity}s · {_fmt(np.nanmin(value.values))} … {_fmt(np.nanmax(value.values))}"
        return f"{len(value.labels)} {value.entity}s · {len(value.components)} components"
    if isinstance(value, EntitySet):
        return f"{len(value.labels)} {value.kind}s"
    if isinstance(value, list) and value and all(isinstance(v, Series) for v in value):
        return f"{len(value)} curves · {len(value[0].x)} points"
    if isinstance(value, list) and value and all(isinstance(v, Number) for v in value):
        vals = [v.value for v in value]
        return f"{len(value)} numbers · {_fmt(min(vals))} … {_fmt(max(vals))}"
    if isinstance(value, list) and value and all(isinstance(v, Bool) for v in value):
        return f"{sum(1 for v in value if v.value)} Yes of {len(value)}"
    if isinstance(value, Bool):
        return "Yes" if value.value else "No"
    if isinstance(value, Text):
        # whole (paths, messages): the editor clamps it to two lines unless the node is widened
        return f"'{value.value}'" if len(value.value) <= 400 else f"'{value.value[:397]}…'"
    if isinstance(value, list) and value and all(isinstance(v, Text) for v in value):
        return f"{len(value)} texts"
    if isinstance(value, list) and value and all(isinstance(v, Field) for v in value):
        return f"{len(value)} fields · {value[0].name}"
    if isinstance(value, list):
        return f"{len(value)} items"
    if isinstance(value, Series):
        return f"{len(value.x)} points · peak {_fmt(float(np.max(np.abs(value.y)))) if len(value.y) else '—'}"
    if isinstance(value, Number):
        return f"{_fmt(value.value)}" + (f" @ {value.where}" if value.where else "")
    return ""
