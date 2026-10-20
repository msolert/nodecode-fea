"""What each port expects, shown in the editor's "?" panel and port tooltips."""

from __future__ import annotations

from .core import REGISTRY
from .maths import FORMULA_NAMES, FORMULA_OUTPUTS


# ------------------------------------------------------------------ docs
# What each port expects, shown in the editor's "?" panel and port tooltips.
PORT_HELP: dict[str, dict[str, str]] = {
    "odb.field": {"odb": "An ODB from Open ODB (or a path).",
                  "where": "Optional: only these nodes/elements: a Set, a Display Group's Group, Compare's Passing, "
                           "a Reduce Max/Min result, or a list of them. Node sets on element fields keep the elements "
                           "touching them, and vice versa.",
                  "field": "The variable (or its Quantity) at every node or element, or only Where."},
    "odb.set": {"odb": "An ODB from Open ODB.", "name": "Optional: the set's name as text (overrides the setting).",
                "set": "Node or element labels: use it as Where (Field Output, Over Time), in Set Operation…"},
    "view.group": {"members": "Nodes or elements: sets, fields (their rows), Reduce Max/Min results, lists of them.",
                   "odb": "Optional: the ODB, to highlight the group on the model here.",
                   "set": "The same nodes/elements as a set, e.g. into Field Output's Where to see a field only "
                          "on this group (in CAE too)."},
    "odb.history": {"odb": "An ODB from Open ODB.", "series": "The curve over step time."},
    "field.invariant": {"field": "A tensor field (S, E, LE…) for Mises/principals, or a vector (U, RF) for Magnitude.",
                        "field_out": "One value per node/element."},
    "field.component": {"field": "Any field from Field Output.", "field_out": "The chosen component only."},
    "field.filter": {"field": "Any field.",
                     "set": "A node or element set. Node sets on element fields (S, E) keep the elements touching "
                            "those nodes; element sets on nodal fields (U, RF) keep those elements' nodes.",
                     "field_out": "The field restricted to the set."},
    "field.expression": {"field": "A scalar field (after Invariant or Component).",
                         "a": "Optional number, usable as a in the formula.", "field_out": "f(x) at every row."},
    "field.from_set": {"odb": "An ODB from Open ODB (for the mesh).",
                       "set": "One or more node sets, or one or more element sets (Set, Picked in CAE, a list…).",
                       "a": "Optional: a number, or a list with one number per set or per member, used as a.",
                       "field": "One value per node/element, usable like any field."},
    "field.reduce": {"field": "A scalar field (after Invariant or Component).",
                     "value": "One number, remembering where it was found, so Contour Plot can mark it."},
    "field.over_frames": {"odb": "An ODB from Open ODB.", "set": "Optional: only consider this set.",
                          "series": "One point per frame."},
    "field.probe": {"odb": "An ODB from Open ODB.", "set": "Optional: probe every node/element of this set.",
                    "series": "One curve per label (a list when there are several). Connect to a Chart."},
    "field.path": {"field": "A scalar field (after Invariant or Component).",
                   "series": "Value vs. distance along the path. Connect to a Chart."},
    "series.reduce": {"series": "A single curve.", "value": "One number."},
    "series.xy": {"x": "Curve whose values become the X axis (e.g. displacement).",
                  "y": "Curve whose values become the Y axis (e.g. force). Matched to X by time.",
                  "series": "Y vs. X."},
    "series.expression": {"series": "One or more curves.", "a": "Optional number, usable as a in the formula.",
                          "series_out": "Transformed curves."},
    "math.expression": {"a": "Optional number.", "b": "Optional number.", "c": "Optional number.",
                        "value": "The formula result."},
    "math.number": {"value": "The constant."},
    "math.formula": {**{k: f"Optional: a number, field, curve, list of numbers or yes/no, used as {k}."
                        for k in FORMULA_NAMES},
                     "value": "A field if a field comes in, else a curve if a curve comes in, else a number, list "
                              "or yes/no. Lists of fields/curves give one result per item.",
                     **{k: "A variable the script sets (named in More outputs); a field, curve, number, list or text "
                           "like the Result." for k in FORMULA_OUTPUTS}},
    "view.value": {"value": "A number, or a list of numbers (shown as a table)."},
    "source.files": {"paths": "The file paths (connect to Open ODB's Path).", "count": "How many files."},
    "odb.steps": {"odb": "An ODB from Open ODB.",
                  "names": "Step names (connect to a Step setting: Field Output, All Frames, History Output…).",
                  "frames": "Number of frames in each step.", "count": "How many steps."},
    "odb.frames": {"odb": "An ODB from Open ODB.", "frames": "Frame numbers (connect to Field Output's Frame #).",
                   "times": "The step time of each of those frames (use as X in Numbers to Curve)."},
    "logic.compare": {"a": "A number, or a list of numbers (one answer each).",
                      "b": "Optional: compare with this number instead of the Value setting.",
                      "result": "Yes or No (a list of them for a list)."},
    "logic.gate": {"a": "Yes/No (e.g. from Compare).", "b": "Yes/No (not needed for 'Not a').",
                   "result": "Yes/No."},
    "logic.if": {"condition": "Yes/No, or a list of them (from Compare).",
                 "then": "What to pass on when Yes (anything, a list, or a geometry Region whose members match "
                         "the conditions).",
                 "else": "Optional: what to pass on when No.", "result": "Then or Else."},
    "list.filter": {"list": "A list, or a Field (rows).", "keep": "Optional: one Yes/No per item.",
                    "list_out": "Only the items that pass."},
    "field.where": {"field": "A scalar field (after Invariant or Component).",
                    "field_out": "Only the nodes/elements that pass; connect to Contour Plot, Table, Reduce…"},
    "odb.open": {"path": "Optional: the ODB path as text (overrides the Path setting), e.g. built with Join Text.",
                 "odb": "The opened output database. Connect it to Field Output, Set, History Output…"},
    "text.value": {"text": "The text."},
    "input.integer": {"value": "The whole number."},
    "input.bool": {"value": "Yes or No."},
    "text.join": {"a": "Text, number, Yes/No, or a list of them.", "b": "Optional.", "c": "Optional.", "d": "Optional.",
                  "text": "Everything joined (a list if any input was a list)."},
    "text.replace": {"text": "A text.", "text_out": "The text with Find removed or replaced."},
    "text.format": {"a": "Optional value for {a}.", "b": "Optional value for {b}.", "c": "Optional value for {c}.",
                    "text": "The filled-in template."},
    "text.from": {"value": "Anything.", "text": "It, written as text."},
    "text.to_number": {"text": "A text that holds a number.", "value": "The number."},
    "text.split": {"text": "A text.", "list": "The pieces, as a list of texts."},
    "text.test": {"text": "A text.", "other": "Optional: the text to look for (else the setting).",
                  "result": "Yes or No."},
    "loop.for": {"i": "The loop counter: one number per pass. Connect it to a number input (e.g. Field Output's "
                         "Frame #, or Expression's a).", "count": "How many passes."},
    "loop.foreach": {"list": "A list, a Set or a Field.", "item": "One item per pass (connect to the next node).",
                     "index": "0, 1, 2… matching Item.", "count": "How many items."},
    "loop.accumulate": {"values": "A list of numbers, one per pass (e.g. a Reduce inside the loop).",
                        "running": "The value after each pass.", "total": "The final value."},
    "list.range": {"list": "The numbers. Connect to any number input to run that node once per number."},
    "list.to_series": {"y": "A list of numbers (e.g. one Reduce result per frame).",
                       "x": "Optional: matching X values (e.g. All Frames' Times); else 0, 1, 2…",
                       "series": "A curve for Chart, Series Value, Combine X–Y…"},
    "cae.pick": {"odb": "The ODB shown in CAE (used to find which instance each picked label belongs to).",
                 "set": "The picked nodes or elements, usable anywhere a Set is."},
    "list.merge": {"a": "An item or a list (any kind).", "b": "An item or a list.", "c": "An item or a list.",
                   "d": "An item or a list.", "list": "Everything joined: A's items first, then B's…"},
    "list.item": {"list": "A list (e.g. from Merge or Probe vs Time), a Set, or a Field.",
                  "item": "The chosen item: for a Set, one node/element as a set; for a Field, one row."},
    "list.split": {"list": "A Set or a Field.", "list_out": "One item per member."},
    "list.length": {"list": "Any list, Set or Field.", "value": "Number of items, members or rows."},
    "list.slice": {"list": "Any list, Set or Field.", "list_out": "The chosen part, of the same kind."},
    "list.sort": {"list": "Numbers, curves, a Set or a Field.", "list_out": "Sorted, of the same kind."},
    "list.reverse": {"list": "Any list, Set or Field.", "list_out": "Same items, reversed."},
    "list.stats": {"list": "A list of numbers, e.g. Merge of several Reduce results.",
                   "value": "One number (with its location for Max/Min)."},
    "view.chart": {"series": "One or more curves. You can connect several wires to this input."},
    "view.table": {"field": "Any field."},
    "view.contour": {"field": "A scalar field (after Invariant or Component).",
                     "deform": "Optional: a displacement field (Field Output U) to draw the deformed shape.",
                     "markers": "Optional, several allowed: numbers to show on the plot (results from Reduce are "
                                "marked where they occur; others are listed in the corner), or fields/sets, e.g. "
                                "from Filter by Value, whose nodes/elements are marked (with values when few)."},
}

for _d in REGISTRY.values():
    _docs = PORT_HELP.get(_d.type, {})
    for _p in _d.inputs:
        _p.help = _p.help or _docs.get(_p.name, "")
    for _p in _d.outputs:
        _p.help = _p.help or _docs.get(f"{_p.name}_out", _docs.get(_p.name, ""))
