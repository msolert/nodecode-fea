"""Amplitude and Load / Boundary Condition, with their per-step settings."""

from __future__ import annotations

import math
import re
import numpy as np

from ... import stepwise
from ...values import (AMPLITUDE_KINDS, Amplitude, AnalysisStep, Bool, Condition, DatumPlane, Field, Instance,
    Interaction, Model, Number, Part, Region, Series, Text)
from ..core import Param, Port, register, REGISTRY
from ..common import _is_matrix
from ..field import _field_coordinates, parse_points
from ..lists import _flatten_all
from ..output import _chart_display
from .geometry import _abaqus_name, _model_value_display, _numbered


CONDITION_KINDS = ["Fixed", "Displacement / rotation", "Concentrated force", "Pressure", "Gravity", "Bolt load",
                   "Temperature", "Initial velocity", "Fixed temperature", "Heat flux", "Convection"]
THERMAL_KINDS = ("Fixed temperature", "Heat flux", "Convection")  # on temperature degrees of freedom
SURFACE_KINDS = ("Pressure", "Heat flux", "Convection")  # on faces (a 2D part's edges), with a side
NO_AMPLITUDE = ("Fixed", "Initial velocity", "Convection")
# Fixed: Abaqus' encastre, pinned, symmetry and antisymmetry conditions (the setting, its constant)
FIXES = {"All (encastre)": "", "Pinned (U1, U2, U3)": "PINNED", "X symmetry": "XSYMM", "Y symmetry": "YSYMM",
         "Z symmetry": "ZSYMM", "X antisymmetry": "XASYMM", "Y antisymmetry": "YASYMM", "Z antisymmetry": "ZASYMM"}
CSYS_KINDS = ["Fixed", "Displacement / rotation", "Concentrated force"]


EXPRESSION_NAMES = {"X", "Y", "Z", "pi", "sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh", "exp",
                    "log", "log10", "sqrt", "pow", "abs", "min", "max"}
MAX_FIELD_POINTS = 200000


def _amplitude_table(wired, text: str) -> tuple:
    """((time, factor), ...) from a curve, a matrix or 'time, factor; …' text; an Amplitude node's as it is."""
    if isinstance(wired, Amplitude):
        return wired
    if wired is None and not text.strip():
        return ()
    if isinstance(wired, Series):
        rows = np.column_stack([wired.x, wired.y])
    elif _is_matrix(wired):
        rows = np.array([[float(x.value) for x in r] for r in wired])
    elif wired is None or isinstance(wired, Text):
        rows = parse_points(wired.value if isinstance(wired, Text) else text, 1)[:, :2]
    else:
        raise ValueError("Amplitude takes an Amplitude, or time, factor pairs: a curve, a two-column matrix or text "
                         "0, 0; 1, 1")
    if rows.ndim != 2 or rows.shape[1] < 2 or not len(rows):
        raise ValueError("Amplitude takes time, factor pairs: a curve, a two-column matrix or text 0, 0; 1, 1")
    rows = rows[:, :2]
    if not np.isfinite(rows).all():
        raise ValueError("Amplitude has values that are infinite or NaN")
    if np.any(np.diff(rows[:, 0]) < 0) or rows[0, 0] < 0:
        raise ValueError("Amplitude times must start at 0 or later and never go back")
    return tuple((float(t), float(a)) for t, a in rows)


def _distribution(wired, text: str) -> tuple:
    """A spatially varying factor: ("expression", text) or ("points", ((x, y, z, value), ...))."""
    if wired is None and not text.strip():
        return ()
    if isinstance(wired, Field):
        if wired.values.shape[1] != 1:
            raise ValueError(f"Distribution needs one value per node/element: pick a component or invariant of "
                             f"'{wired.name}' with Quantity")
        rows = np.column_stack([_field_coordinates(wired), wired.values[:, 0]])
    elif _is_matrix(wired):
        rows = np.array([[float(x.value) for x in r] for r in wired])
        if rows.ndim != 2 or rows.shape[1] != 4:
            raise ValueError("Distribution points are x, y, z, value rows (4 columns)")
    elif wired is None or isinstance(wired, Text):
        raw = (wired.value if isinstance(wired, Text) else text).strip()
        try:  # numbers only: x, y, z, value rows typed in
            rows = np.array([[float(c) for c in re.split(r"[,\s]+", r.strip()) if c]
                             for r in re.split(r"[;\n]+", raw) if r.strip()])
        except ValueError:
            unknown = sorted(set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", raw)) - EXPRESSION_NAMES)
            if unknown:
                raise ValueError(f"Distribution expression: unknown name '{unknown[0]}' (use X, Y, Z, pi and "
                                 "functions like sin, cos, exp, sqrt, pow, abs)")
            if not re.fullmatch(r"[A-Za-z0-9_+\-*/().,\s]+", raw):
                raise ValueError("Distribution expression: use numbers, X, Y, Z, + - * / ( ) and functions")
            return ("expression", raw)
        if rows.ndim != 2 or rows.shape[1] != 4:
            raise ValueError("Distribution points are x, y, z, value rows, e.g. 0, 0, 0, 1; 100, 0, 0, 2")
    else:
        raise ValueError("Distribution takes an expression in X, Y, Z, x, y, z, value rows, or a result Field")
    rows = rows[np.isfinite(rows).all(axis=1)]
    if not len(rows):
        raise ValueError("Distribution has no finite values")
    if len(rows) > MAX_FIELD_POINTS:
        raise ValueError(f"Distribution has {len(rows)} points; the limit is {MAX_FIELD_POINTS}")
    return ("points", tuple(tuple(round(float(c), 9) for c in r) for r in rows))


def _region_part(region) -> Part | None:
    """The part a Region is on (through its instance)."""
    source = region.source if isinstance(region, Region) else None
    return source.part if isinstance(source, Instance) else source


def _condition_atom(value):
    """Plain value from an Input matrix cell."""
    if isinstance(value, (Number, Text, Bool)):
        return value.value
    return value


def _condition_number(value, label: str) -> float:
    try:
        number = float(_condition_atom(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _condition_displacement(value, label: str) -> float | None:
    raw = str(_condition_atom(value)).strip()
    if not raw or raw.lower() in ("free", "unset", "none"):
        return None
    return _condition_number(raw, label)


def _validate_condition_values(kind: str, values: tuple) -> tuple:
    if kind == "Displacement / rotation" and not any(v is not None for v in values):
        raise ValueError("Displacement / rotation needs at least one prescribed component; use Fixed to clamp all")
    if kind in ("Concentrated force", "Gravity") and not any(values):
        raise ValueError(f"{kind} components cannot all be zero")
    if kind == "Bolt load" and values[0] == 0:
        raise ValueError("Bolt force can't be 0")
    return values


# The settings of each kind that make its values (in order), all of them per-step settings.
CONDITION_SETTINGS = {"Fixed": (), "Displacement / rotation": ("u1", "u2", "u3", "ur1", "ur2", "ur3"),
                      "Concentrated force": ("x", "y", "z"), "Pressure": ("magnitude",), "Gravity": ("x", "y", "z"),
                      "Bolt load": ("force",), "Temperature": ("temperature",), "Initial velocity": ("x", "y", "z"),
                      "Fixed temperature": ("temperature",), "Heat flux": ("flux",), "Convection": ("film", "sink")}
SETTING_LABELS = {"u1": "U1", "u2": "U2", "u3": "U3", "ur1": "UR1", "ur2": "UR2", "ur3": "UR3", "x": "X", "y": "Y",
                  "z": "Z", "magnitude": "Pressure", "force": "Bolt force", "temperature": "Temperature",
                  "friction": "Friction coefficient", "amplitude": "Amplitude", "flux": "Heat flux",
                  "film": "Film coefficient", "sink": "Sink temperature"}
# made in Initial, or may be: boundary conditions and predefined fields (loads start in an analysis step)
IN_INITIAL = ("Fixed", "Displacement / rotation", "Temperature", "Initial velocity", "Fixed temperature")


def _condition_setting(kind: str, key: str, raw, label: str):
    return (_condition_displacement(raw, label) if kind == "Displacement / rotation" else
            _condition_number(raw, label))


def _stepwise_values(kind: str, params: dict) -> tuple[tuple, tuple]:
    """The kind's values where it is created, and their changes by step: (values, ((setting, ((step, value),
    ...)), ...)). An empty base takes the setting's default."""
    defaults = {p.name: p.default for p in REGISTRY["model.condition"].params}
    values, changes = [], []
    for key in CONDITION_SETTINGS[kind]:
        label = SETTING_LABELS[key]
        base, steps = stepwise.split(params.get(key, defaults[key]))
        values.append(_condition_setting(kind, key, base if base != "" else defaults[key], label))
        if steps:
            changes.append((key, tuple((s, _condition_setting(kind, key, v, f"{label} in {s}")) for s, v in steps)))
    return tuple(values), tuple(changes)


STEP_TABLE_HELP = ("A table from another node (an Input matrix, a Formula, text rows): a header row Step, Active and "
                   "the setting columns (e.g. Step, Active, Z, Amplitude), then one row per step. Active is Yes/No "
                   "(1/0); an empty cell keeps the step before's value. Its values win over typed ones in the same "
                   "step; its Active column replaces the ticks.")


def _step_table(wired, columns: dict, convert) -> tuple[tuple | None, tuple]:
    """A wired Per step table as (Active in items or None, changes): columns maps lower-cased header names (setting
    names and labels) to settings; convert(setting, text, label) reads a cell."""
    if wired is None:
        return None, ()
    if isinstance(wired, Text):
        rows = [[c.strip() for c in r.split(",")] for r in re.split(r"[;\n]+", wired.value) if r.strip()]
    elif isinstance(wired, list) and wired and all(isinstance(r, list) for r in wired):
        rows = [[_condition_atom(c) for c in r] for r in wired]
    else:
        raise ValueError("Per step takes a table: a header row (Step, Active, settings…) and one row per step")
    header = [str(c).strip().lower() for c in rows[0]] if rows else []
    if len(rows) < 2 or not header or header[0] != "step":
        raise ValueError("Per step table: the first row names the columns, starting with Step (e.g. Step, Active, Z), "
                         "then one row per step")
    keys = []
    for name in header[1:]:
        if name not in columns:
            raise ValueError(f"Per step table: no setting '{name}' here (columns: Step, "
                             f"{', '.join(dict.fromkeys(columns.values()))})")
        keys.append(columns[name])
    active, changes = [], {k: [] for k in keys if k != "active"}
    for row in rows[1:]:
        step = str(row[0]).strip()
        if not step:
            continue
        for key, cell in zip(keys, row[1:]):
            text = "" if cell is None else str(cell).strip()
            if key == "active":
                if text.lower() in ("yes", "1", "1.0", "true", "on", "x", "active"):
                    active.append(("step", step))
                elif text.lower() not in ("", "no", "0", "0.0", "false", "off", "-"):
                    raise ValueError(f"Per step table: Active in {step} is Yes or No, not '{text}'")
            elif text:
                changes[key].append((step, convert(key, text, f"{SETTING_LABELS.get(key, key)} in {step}")))
    if "active" in keys and not active:
        raise ValueError("Per step table: Active is No in every step")
    return (tuple(active) if "active" in keys else None), tuple((k, tuple(v)) for k, v in changes.items() if v)


def _table_columns(keys: tuple, params: list) -> dict:
    """Header names a Per step table may use for these settings: their names and labels, any case."""
    labels = {p.name: p.label for p in params}
    out = {}
    for key in keys:
        for alias in (key, labels.get(key, ""), SETTING_LABELS.get(key, "")):
            if alias:
                out[alias.lower()] = key
    return out


def _amplitude_steps(wired, text: str) -> tuple[tuple, tuple]:
    """Amplitude where the condition is created (a wired curve or the typed rows) and its changes by step; an empty
    step value goes back to Abaqus' default (ramped/instant)."""
    base, steps = stepwise.split(text)
    return (_amplitude_table(wired, base),
            (("amplitude", tuple((s, _amplitude_table(None, v)) for s, v in steps)),) if steps else ())


def _check_stepwise(value, steps_by_model: dict) -> tuple:
    """Late check, once every Step of the graph is known: its step names and Active in fit the model's steps.
    Returns what the editor's Per-step table shows, also when they don't (so it stays to fix them): the steps
    (Initial only when it can be there), those it is active in, the values per step, and the error or ''."""
    if isinstance(value, list):
        return _check_stepwise(value[0], steps_by_model) if value else ([], [], {}, "")
    if isinstance(value, Condition):
        start = value.step.name
        names = steps_by_model.get(value.step.model.name, [stepwise.INITIAL, start])
        initial = value.kind in IN_INITIAL
        if value.kind in ("Temperature", "Initial velocity") and not value.active:
            start = stepwise.INITIAL
    elif isinstance(value, Interaction):
        start = value.step.name if value.step is not None else stepwise.INITIAL
        names = steps_by_model.get(value.model.name, [stepwise.INITIAL] + ([start] if value.step else []))
        initial = value.kind not in ("Frictionless contact", "Frictional contact")
        if initial:
            start = stepwise.INITIAL
    else:
        return [], [], {}, ""
    if start not in names:
        names = names + [start]
    error = ""
    try:
        if value.stepwise:
            stepwise.plan(names, start, value.active, {}, value.changes, value.name, initial)
        mask = stepwise.active_mask(value.active, names, start)
    except ValueError as exc:
        error = str(exc)
        mask = stepwise.active_mask((), names, start)
    listed = names if initial else names[1:]
    return listed, [n for n, on in zip(names, mask) if on and n in listed], _cells(value, names, mask), error


def _cell_text(v) -> str:
    if v is None:
        return "Free"
    if isinstance(v, Amplitude):
        return v.summary()
    if isinstance(v, tuple):
        return "; ".join(f"{t:g}, {a:g}" for t, a in v) if v else "default"
    return f"{v:.12g}"


def _cells(value, names: list[str], mask: list[bool]) -> dict:
    """{setting: {step: text}}: the value in effect in each step it is active in (the Per step table shows it)."""
    if isinstance(value, Condition):
        current = dict(zip(CONDITION_SETTINGS[value.kind], value.values), amplitude=value.amplitude)
    else:
        current = {"friction": value.friction}
    by_step: dict[str, dict] = {}
    for key, seq in value.changes:
        for step, v in seq:
            by_step.setdefault(step.casefold(), {})[key] = v
    out: dict[str, dict] = {k: {} for k in current}
    for name, on in zip(names, mask):
        current.update(by_step.get(name.casefold(), {}))
        if on:
            for k, v in current.items():
                out[k][name] = _cell_text(v)
    return out


POINT_AMPLITUDES = ["Tabular", "Smooth step"]
SMOOTHED_AMPLITUDES = ["Tabular", "Equally spaced"]
STARTED_AMPLITUDES = ["Periodic", "Modulated", "Exponential decay"]  # A0 until a start time


def _amplitude_numbers(text: str, label: str, columns: int) -> list:
    """Rows of numbers from 'a, b; c, d' text (columns 1: one flat list, any separators)."""
    try:
        rows = [[float(c) for c in re.split(r"[,\s]+", r.strip()) if c] for r in re.split(r"[;\n]+", text) if r.strip()]
    except ValueError as exc:
        raise ValueError(f"{label}: use numbers separated by , and ;") from exc
    if columns == 1:
        return [v for r in rows for v in r]
    if any(len(r) != columns for r in rows):
        raise ValueError(f"{label}: {columns} numbers per row, rows separated by ;")
    return rows


def _amplitude_value(kind: str, inputs: dict, params: dict) -> Amplitude:
    def num(key, label, positive=False):
        v = float(params[key])
        if not math.isfinite(v):
            raise ValueError(f"{label} must be finite")
        if positive and v <= 0:
            raise ValueError(f"{label} must be greater than 0")
        return v

    span = "Total" if str(params.get("time_span") or "Step time") == "Total time" else "Step"
    smooth = None
    if kind in SMOOTHED_AMPLITUDES:
        raw = str(params.get("smoothing") or "").strip()
        if raw and raw.lower() not in ("default", "solver default"):
            smooth = _condition_number(raw, "Smoothing")
            if not 0 <= smooth <= 0.5:
                raise ValueError("Smoothing is between 0 and 0.5 (Abaqus' default: 0.05)")
    if kind in POINT_AMPLITUDES:
        data = _amplitude_table(inputs.get("points"), str(params.get("points") or ""))
        if not data:
            raise ValueError(f"{kind} amplitude needs time, value points, e.g. 0, 0; 1, 1")
        return Amplitude(kind, data, (), span, smooth)
    if kind == "Equally spaced":
        wired = inputs.get("points")
        if isinstance(wired, Series):
            values = [float(v) for v in wired.y]
        elif isinstance(wired, list):
            values = [_condition_number(v, "Values") for v in _flatten_all(wired)]
        elif wired is None or isinstance(wired, Text):
            values = _amplitude_numbers(wired.value if isinstance(wired, Text) else str(params.get("values") or ""),
                                        "Values", 1)
        else:
            raise ValueError("Equally spaced values: a list of numbers, a curve (its y) or text 0, 0.5, 1")
        if len(values) < 2 or not all(math.isfinite(v) for v in values):
            raise ValueError("Equally spaced amplitude needs at least two finite values, e.g. 0, 0.5, 1, 0.5")
        settings = (("fixedInterval", num("interval", "Time interval", True)), ("begin", num("begin", "Begin time")))
        if settings[1][1] < 0:
            raise ValueError("Begin time must be 0 or later")
        return Amplitude(kind, tuple(values), settings, span, smooth)
    if kind == "Periodic":
        terms = inputs.get("points")
        if terms is not None and _is_matrix(terms):
            rows = [[_condition_number(c, "Fourier terms") for c in r] for r in terms]
        else:
            rows = _amplitude_numbers(terms.value if isinstance(terms, Text) else str(params.get("terms") or ""),
                                      "Fourier terms", 2)
        if not rows or any(len(r) != 2 for r in rows):
            raise ValueError("Periodic amplitude needs Fourier terms: A, B pairs, e.g. 0, 1 for sin(ωt)")
        settings = (("frequency", num("frequency", "Circular frequency ω", True)), ("start", num("start", "Start time")),
                    ("a_0", num("initial", "Initial value A0")))
        return Amplitude(kind, tuple((float(a), float(b)) for a, b in rows), settings, span)
    if kind == "Modulated":
        return Amplitude(kind, (), (("initial", num("initial", "Initial value A0")),
                                    ("magnitude", num("magnitude", "Magnitude A")),
                                    ("start", num("start", "Start time")),
                                    ("frequency1", num("frequency1", "Circular frequency ω1", True)),
                                    ("frequency2", num("frequency2", "Circular frequency ω2", True))), span)
    if kind == "Exponential decay":
        return Amplitude(kind, (), (("initial", num("initial", "Initial value A0")),
                                    ("maximum", num("magnitude", "Magnitude A")),
                                    ("start", num("start", "Start time")),
                                    ("decayTime", num("decay", "Decay time", True))), span)
    if kind == "Solution dependent":
        initial, low, high = (num("factor", "Initial factor", True), num("minimum", "Minimum", True),
                              num("maximum", "Maximum", True))
        if not low <= initial <= high:
            raise ValueError(f"Solution dependent: Minimum ≤ Initial factor ≤ Maximum ({low:g}, {initial:g}, {high:g})")
        return Amplitude(kind, (), (("initial", initial), ("minimum", low), ("maximum", high)), span)
    if kind == "User":
        variables = int(float(params.get("variables") or 0))
        if variables < 0:
            raise ValueError("Variables must be 0 or more")
        return Amplitude(kind, (), (("numVariables", float(variables)),), span)
    raise ValueError(f"Amplitude type is one of {', '.join(AMPLITUDE_KINDS)}")


def _amplitude_curve(amp: Amplitude, until: float) -> Series | None:
    """The amplitude over time, for plotting (None for a user subroutine's)."""
    pts = amp.points()
    end = until if until > 0 else (pts[-1][0] if pts else 1.0)
    start = min(0.0, pts[0][0]) if pts else 0.0
    if pts and amp.kind == "Tabular":
        t = np.array(sorted({start, end, *(p[0] for p in pts if p[0] <= end)}))
    else:
        t = np.linspace(start, end, 801)
    a = amp.sample(t)
    if a is None:
        return None
    time = "total time" if amp.time_span == "Total" else "step time"
    return Series(f"{amp.kind} amplitude", t, np.asarray(a, dtype=float), time, "amplitude")


def _amplitude_display(o: dict) -> dict:
    curve = o.get("curve")
    if isinstance(curve, Series):
        return _chart_display([curve])
    amp = o["amplitude"]
    return {"kind": "info", "title": amp.summary(), "items": [
        [k, f"{v:g}"] for k, v in amp.settings] + [["Curve", "Given by your UAMP / VUAMP subroutine (Job: User "
                                                             "subroutine)" if amp.kind == "User" else ""]]}


@register("model.amplitude", "Amplitude", "Model",
          "A time history (load curve, time function) for a Load / Boundary Condition: every Abaqus amplitude type. "
          "Tabular (points, a ramp), Smooth step (smooth ramp, Explicit), Equally spaced (values every Δt), Periodic "
          "(Fourier series: sine, cosine, cyclic, harmonic, vibration), Modulated (beat, sin × sin), Exponential decay "
          "(impulse, blast), Solution dependent (superplastic forming, creep strain rate control) and User (UAMP / "
          "VUAMP subroutine). Over step or total time. Wire it into a condition's Amplitude (several conditions can "
          "share it); Curve plots it.",
          inputs=[Port("points", "any", "Points / values", optional=True, depth=0, table=True,
                       when={"kind": POINT_AMPLITUDES + ["Equally spaced", "Periodic"]},
                       help="Tabular / Smooth step: time, value pairs (a curve, a two-column matrix or text). "
                            "Equally spaced: the values (a list, a curve's y or text). Periodic: A, B rows (a matrix "
                            "or text). Overrides what is typed.")],
          outputs=[Port("amplitude", "amplitude", "Amplitude"), Port("curve", "series", "Curve")],
          params=[Param("kind", "select", "Tabular", "Type", options=AMPLITUDE_KINDS,
                        help="Tabular: straight lines between points. Smooth step: smooth (zero slope) between them. "
                             "Equally spaced: values at a fixed time interval. Periodic: A0 + Σ An cos nω(t−t0) + "
                             "Bn sin nω(t−t0). Modulated: A0 + A sin ω1(t−t0) sin ω2(t−t0). Exponential decay: A0 + "
                             "A exp(−(t−t0)/td). Solution dependent: Abaqus scales it to keep the creep strain rate "
                             "(superplastic forming). User: a UAMP (Standard) / VUAMP (Explicit) subroutine."),
                  Param("time_span", "select", "Step time", "Time", options=["Step time", "Total time"],
                        help="Step time: each step's own time from 0. Total time: the analysis' time, across steps."),
                  Param("points", "text", "0, 0; 1, 1", "Points", when={"kind": POINT_AMPLITUDES},
                        help="Time, value pairs, e.g. 0, 0; 0.5, 1; 1, 0.2. Abaqus holds the last value after them."),
                  Param("values", "text", "0, 1, 0", "Values", when={"kind": ["Equally spaced"]},
                        help="The values, one every Time interval from Begin time, e.g. 0, 0.5, 1, 0.5"),
                  Param("interval", "number", 0.1, "Time interval", when={"kind": ["Equally spaced"]}),
                  Param("begin", "number", 0.0, "Begin time", when={"kind": ["Equally spaced"]}),
                  Param("smoothing", "text", "Default", "Smoothing", when={"kind": SMOOTHED_AMPLITUDES},
                        help="How much Abaqus rounds the corners between points: 0 to 0.5 (Default: 0.05)"),
                  Param("frequency", "number", 6.283185307, "Circular frequency ω", when={"kind": ["Periodic"]},
                        help="In radians per time unit: 2π × frequency in Hz (6.2832 is 1 Hz)"),
                  Param("terms", "text", "0, 1", "Fourier terms", when={"kind": ["Periodic"]},
                        help="A, B per harmonic n = 1, 2…: An cos nω(t−t0) + Bn sin nω(t−t0). 0, 1 is sin ωt; "
                             "1, 0 is cos ωt; 0, 1; 0, 0.3 adds a second harmonic."),
                  Param("initial", "number", 0.0, "Initial value A0", when={"kind": STARTED_AMPLITUDES}),
                  Param("magnitude", "number", 1.0, "Magnitude A", when={"kind": ["Modulated", "Exponential decay"]}),
                  Param("start", "number", 0.0, "Start time t0", when={"kind": STARTED_AMPLITUDES},
                        help="Before it, the amplitude is A0"),
                  Param("frequency1", "number", 6.283185307, "Circular frequency ω1", when={"kind": ["Modulated"]}),
                  Param("frequency2", "number", 62.83185307, "Circular frequency ω2", when={"kind": ["Modulated"]}),
                  Param("decay", "number", 0.1, "Decay time td", when={"kind": ["Exponential decay"]}),
                  Param("factor", "number", 1.0, "Initial factor", when={"kind": ["Solution dependent"]}),
                  Param("minimum", "number", 0.1, "Minimum", when={"kind": ["Solution dependent"]}),
                  Param("maximum", "number", 1000.0, "Maximum", when={"kind": ["Solution dependent"]}),
                  Param("variables", "int", 0, "State variables", when={"kind": ["User"]},
                        help="How many solution-dependent variables the subroutine keeps"),
                  Param("until", "number", 0.0, "Curve until",
                        help="The time the Curve output (and the plot) runs to. 0: the last point, or 1.")],
          display=lambda o, i, p: _amplitude_display(o))
def model_amplitude(inputs, params):
    amp = _amplitude_value(str(params.get("kind") or "Tabular"), inputs, params)
    until = float(params.get("until") or 0.0)
    if not math.isfinite(until) or until < 0:
        raise ValueError("Curve until must be 0 or a time after 0")
    out = {"amplitude": amp}
    curve = _amplitude_curve(amp, until)
    if curve is not None:
        out["curve"] = curve
    return out


@register("model.condition", "Load / Boundary Condition", "Model",
          "Apply a load, boundary condition or predefined field in an analysis Step: fixed, prescribed "
          "displacement/rotation, concentrated force, pressure, gravity, a bolt load (bolt pretension, preload) "
          "across a bolt's cut face, a temperature field (thermal load, initial temperature) or an initial velocity; "
          "symmetry, antisymmetry and pinned supports (XSYMM, YSYMM, ZSYMM: half and quarter models); in a heat "
          "transfer or coupled step a fixed temperature, a heat flux or convection (film coefficient, sink "
          "temperature). A coordinate system Datum turns displacements and forces into local (e.g. cylindrical: "
          "radial) directions. "
          "Feed another condition to add several in the same step. Every value can change from step to step "
          "(load cases, load history: 'value; Step: value', or the Per-step table) and Active in turns it on and off "
          "(deactivate, remove, switch off and on again in later steps). "
          "Regions select geometry; lists of regions or magnitudes create corresponding conditions. Amplitude "
          "varies it over time (a time history, load curve, ramp or smooth step typed here; an Amplitude node for "
          "every other type: periodic, cyclic, decay, modulated…); Distribution varies it "
          "over space (a non-uniform, spatially varying field: an expression in X, Y, Z, values at points, or a "
          "result field from another ODB, mapped onto this mesh).",
          inputs=[Port("step", "any", "Step or previous condition", depth=0,
                       help="Where it is created (its first step), unless Active in says otherwise. A temperature "
                            "field starts in Initial; the Step tells it the model."),
                  Port("region", "any", "Region", optional=True, depth=0,
                       help="Required except for Gravity. A coupling (Interaction) means its reference point."),
                  Port("steps", "any", "Per step", optional=True, depth=2, table=True, help=STEP_TABLE_HELP),
                  Port("csys", "datum", "Coordinate system", optional=True, depth=0, when={"kind": CSYS_KINDS},
                       help="A Datum of type Coordinate system in the assembly (made from an Instance or Model): the "
                            "components (U1, X…) are along its axes. Not wired: global X, Y, Z."),
                  Port("amplitude", "any", "Amplitude", optional=True, depth=0, table=True,
                       when={"kind": [k for k in CONDITION_KINDS if k not in NO_AMPLITUDE]},
                       help="An Amplitude (any type: periodic, decay, smooth step…), or time, factor pairs: a "
                            "curve (e.g. Make Curve or a Formula over time), a matrix or text. Overrides the typed "
                            "Amplitude where the condition is created."),
                  Port("distribution", "any", "Distribution", optional=True, depth=0, table=True,
                       when={"kind": ["Displacement / rotation", "Concentrated force", "Pressure", "Temperature"]},
                       help="Factor over space: an expression in X, Y, Z (text), x, y, z, value rows (a matrix), "
                            "or a scalar result Field, e.g. from another analysis' ODB, mapped onto this mesh.")],
          outputs=[Port("condition", "condition", "Condition")],
          params=[Param("name", "text", "Condition-1", "Name"),
                  Param("kind", "select", "Fixed", "Kind", options=CONDITION_KINDS),
                  Param("fix", "select", "All (encastre)", "Fixed", options=list(FIXES), when={"kind": ["Fixed"]},
                        help="All: every displacement and rotation (encastre, clamped). Pinned: displacements only. "
                             "X symmetry: the region is on a symmetry plane normal to X (U1 = UR2 = UR3 = 0), and so "
                             "on; antisymmetry keeps the other components (U2 = U3 = UR1 = 0)."),
                  Param("active", "text", "", "Active in", per_step=True, table_only=True,
                        when={"kind": [k for k in CONDITION_KINDS if k != "Initial velocity"]},
                        help="The steps it applies in: names and ranges, e.g. Zenith, Wind or Load to Unload or "
                             "Wind to end (Initial too for boundary conditions and temperature). Empty: from its "
                             "Step to the end (a temperature: from Initial). Off in a step deactivates it there; on "
                             "again later makes a fresh copy (<name>-from-<step>)."),
                  Param("u1", "text", "Free", "U1", per_step=True, when={"kind": ["Displacement / rotation"]}),
                  Param("u2", "text", "Free", "U2", per_step=True, when={"kind": ["Displacement / rotation"]}),
                  Param("u3", "text", "Free", "U3", per_step=True, when={"kind": ["Displacement / rotation"]}),
                  Param("ur1", "text", "Free", "UR1", per_step=True, when={"kind": ["Displacement / rotation"]}),
                  Param("ur2", "text", "Free", "UR2", per_step=True, when={"kind": ["Displacement / rotation"]}),
                  Param("ur3", "text", "Free", "UR3", per_step=True, when={"kind": ["Displacement / rotation"]}),
                  Param("x", "number", 0.0, "X component", per_step=True,
                        when={"kind": ["Concentrated force", "Gravity", "Initial velocity"]}),
                  Param("y", "number", 0.0, "Y component", per_step=True,
                        when={"kind": ["Concentrated force", "Gravity", "Initial velocity"]}),
                  Param("z", "number", -1.0, "Z component", per_step=True,
                        when={"kind": ["Concentrated force", "Gravity", "Initial velocity"]},
                        help="A value per step: -9810; Horizon: 0 (the value from where it is created, then a "
                             "Step: value change holding until the next)."),
                  Param("magnitude", "number", 1.0, "Pressure", per_step=True, when={"kind": ["Pressure"]}),
                  Param("force", "number", 1000.0, "Bolt force", per_step=True, when={"kind": ["Bolt load"]},
                        help="The pretension across the Region: one face cutting the bolt's shank (partition the "
                             "bolt with a Datum plane and Part Feature, then Find at a point on that face, on the "
                             "instance). Its axis is the face's normal. Per step, e.g. 10000; Retighten: 15000."),
                  Param("then", "select", "Fix its length", "In later steps", options=["Fix its length", "Keep the force"],
                        when={"kind": ["Bolt load"]},
                        help="Fix its length: the bolt keeps the length the preload gave it, as a real tightened bolt "
                             "(its force then changes with the load). Keep the force: the same force throughout. A "
                             "step with its own Bolt force applies that force there."),
                  Param("temperature", "number", 20.0, "Temperature", per_step=True,
                        when={"kind": ["Temperature", "Fixed temperature"]},
                        help="Temperature: the initial temperature, then per step: 20; Heat: 200; Cool: 20; the "
                             "Region's nodes take it (a predefined field: thermal expansion needs Expansion on the "
                             "material). Fixed temperature: held there (a boundary condition of a heat transfer)."),
                  Param("flux", "number", 1.0, "Heat flux", per_step=True, when={"kind": ["Heat flux"]},
                        help="Heat per area and time into the faces (power per area: W/m² in SI, mW/mm² in mm)."),
                  Param("film", "number", 1e-5, "Film coefficient", per_step=True, when={"kind": ["Convection"]},
                        help="Heat transfer coefficient h of the convection (W/(m²·K) in SI, mW/(mm²·K) in mm: "
                             "air about 1e-5 to 1e-4, water 1e-3)."),
                  Param("sink", "number", 20.0, "Sink temperature", per_step=True, when={"kind": ["Convection"]},
                        help="The surrounding fluid's temperature the faces exchange heat with."),
                  Param("amplitude", "text", "", "Amplitude", per_step=True,
                        when={"kind": [k for k in CONDITION_KINDS if k not in NO_AMPLITUDE]},
                        help="Time, factor pairs, e.g. 0, 0; 0.5, 1; 1, 0.2 (step time). Empty: Abaqus' default "
                             "(ramped in static steps). Per step: 0, 0; 1, 1; Wind: 0, 1; 1, 0 (Wind: alone goes "
                             "back to the default)."),
                  Param("amplitude_shape", "select", "Tabular", "Amplitude shape", options=["Tabular", "Smooth step"],
                        when={"kind": [k for k in CONDITION_KINDS if k not in NO_AMPLITUDE]},
                        help="Tabular: straight lines between the points. Smooth step: smooth transitions."),
                  Param("distribution", "text", "", "Distribution",
                        when={"kind": ["Displacement / rotation", "Concentrated force", "Pressure", "Temperature"]},
                        help="Factor over space multiplying the magnitude: an expression in X, Y, Z like "
                             "1 + 0.01 * X or sin(pi * Y / 50), or x, y, z, value rows. Empty: uniform.")],
          display=lambda o, i, p: _model_value_display(o["condition"]), late=_check_stepwise)
def model_condition(inputs, params):
    upstream = inputs["step"]
    previous = upstream if isinstance(upstream, Condition) else None
    step = upstream.step if previous is not None else upstream
    if not isinstance(step, AnalysisStep):
        raise ValueError("Load / Boundary Condition needs a Step or previous condition")
    kind, region = str(params["kind"]), inputs.get("region")
    if step.kind == "Frequency" and kind not in ("Temperature", "Initial velocity") and not params.get("active"):
        raise ValueError("Loads and boundary conditions cannot be created in a Frequency perturbation step")
    if kind in THERMAL_KINDS and not step.thermal:
        raise ValueError(f"{kind} acts on temperatures: it needs a Heat transfer or Coupled temperature-displacement "
                         f"step, not {step.kind}")
    if step.kind == "Heat transfer" and kind not in THERMAL_KINDS + ("Temperature",):
        raise ValueError(f"A Heat transfer step has temperatures only: {kind} needs a structural or Coupled "
                         "temperature-displacement step")
    if kind == "Gravity":
        region = None
    coupled = isinstance(region, Interaction) and region.point_owner
    if isinstance(region, Interaction) and not coupled:
        raise ValueError(f"{kind} takes a Region or a coupling (its reference point), not a {region.kind}")
    if kind != "Gravity" and not (isinstance(region, Region) or coupled):
        raise ValueError(f"{kind} needs a Region (or a coupling, for its reference point)")
    if coupled:
        if region.model != step.model:
            raise ValueError("The coupling and Step belong to different models")
        if kind == "Pressure":
            raise ValueError("Pressure needs a Face Region; a coupling's reference point takes forces and "
                             "displacements")
    elif region is not None:
        source = region.source.part if isinstance(region.source, Instance) else region.source
        if source.model != step.model:
            raise ValueError("The Region and Step belong to different models")
    if kind == "Concentrated force" and not coupled and region.kind not in ("vertex", "reference point"):
        raise ValueError("Concentrated force needs a Vertex Region, a reference point or a coupling")
    flat = region is not None and not coupled and getattr(_region_part(region), "flat", False)
    if kind in SURFACE_KINDS and (coupled or region.kind != ("edge" if flat else "face")):
        raise ValueError(f"{kind} needs an Edge Region on a 2D part" if flat else f"{kind} needs a Face Region")
    if kind == "Bolt load":
        if coupled or region.kind != "face":
            raise ValueError("Bolt load needs a Face Region: the face that cuts the bolt's shank")
        if not isinstance(region.source, Instance):
            raise ValueError("Bolt load needs its face on an instance (one bolt each; a list of instances and "
                             "regions makes one bolt load per bolt)")
        if not region.dynamic and len(region.indices or region.points) != 1:
            raise ValueError("Bolt load acts across one face: its Region selects "
                             f"{len(region.indices or region.points)}")
    if kind in ("Temperature", "Initial velocity") and coupled:
        raise ValueError(f"{kind} needs a Region (its nodes take it)")
    values, changes = _stepwise_values(kind, params)
    if kind == "Bolt load":
        values += (1.0 if str(params.get("then") or "Fix its length") == "Fix its length" else 0.0,)
    active = stepwise.parse_active(params.get("active") or "") if kind != "Initial velocity" else ()
    if inputs.get("steps") is not None:
        keys = CONDITION_SETTINGS[kind] + (("amplitude",) if kind not in NO_AMPLITUDE else ()) + \
            (("active",) if kind != "Initial velocity" else ())

        def cell(key, text, label):
            return (_amplitude_table(None, text) if key == "amplitude" else _condition_setting(kind, key, text, label))
        table_active, table_changes = _step_table(inputs["steps"], _table_columns(keys, REGISTRY["model.condition"].params
                                                                                 if "model.condition" in REGISTRY else []), cell)
        active = table_active if table_active is not None else active
        changes += tuple(c for c in table_changes if c[0] != "amplitude")
        table_amp = tuple(c for c in table_changes if c[0] == "amplitude")
    else:
        table_amp = ()
    if kind == "Initial velocity":
        if changes:
            raise ValueError("An initial velocity is given once, in Initial: it can't change by step")
        if not any(values):
            raise ValueError("Initial velocity components cannot all be zero")
    elif not changes and not active:
        _validate_condition_values(kind, values)
    elif kind == "Displacement / rotation":
        _validate_condition_values(kind, values if not changes else (0.0,))
    name = _numbered(_abaqus_name(str(params["name"]), "Condition-1"), params, model=step.model)
    amplitude, amp_changes = ((), ()) if kind in NO_AMPLITUDE else _amplitude_steps(
        inputs.get("amplitude"), str(params.get("amplitude") or ""))
    fix = FIXES.get(str(params.get("fix") or "All (encastre)")) if kind == "Fixed" else ""
    if fix is None:
        raise ValueError(f"Fixed is one of {', '.join(FIXES)}")
    csys = inputs.get("csys") if kind in CSYS_KINDS else None
    if csys is not None:
        if not isinstance(csys, DatumPlane) or csys.kind != "Coordinate system":
            raise ValueError("Coordinate system takes a Datum of type Coordinate system")
        if isinstance(csys.part, Part):
            raise ValueError("Loads and boundary conditions use an assembly coordinate system: make the Datum on an "
                             "Instance or the Model, not on a Part")
        owner = csys.part if isinstance(csys.part, Model) else csys.part.part.model
        if owner != step.model:
            raise ValueError("The coordinate system belongs to another model")
        if kind == "Fixed" and not fix:
            raise ValueError("All (encastre) holds everything, in any directions: a coordinate system is for "
                             "symmetry, pinned, displacements and forces")
    distribution = (_distribution(inputs.get("distribution"), str(params.get("distribution") or ""))
                    if kind in ("Displacement / rotation", "Concentrated force", "Pressure", "Temperature") else ())
    return {"condition": Condition(name, step, kind, region, values, previous, amplitude,
                                   str(params.get("amplitude_shape") or "Tabular"), distribution, active,
                                   changes + amp_changes + table_amp, fix=fix, csys=csys)}
