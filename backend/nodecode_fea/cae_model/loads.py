"""Loads, boundary conditions, predefined fields and their amplitudes."""

from __future__ import annotations

from .. import stepwise
from ..nodes import CONDITION_SETTINGS, IN_INITIAL
from ..values import Amplitude, AnalysisStep, Condition, Interaction
from .core import _n, _pt
from .regions import coupling_point, region_kw_code, REPOS, selection_code


def _condition_step_name(step: AnalysisStep, objects: list[dict]) -> str:
    """A step's final CAE name (including collision renaming)."""
    return next((o["name"] for o in objects if o["value"] is step), step.name)


def model_step_names(model: str, objects: list[dict]) -> tuple[list[str], dict[str, str]]:
    """The model's steps in order (Initial first) as the graph names them, and each one's CAE name."""
    steps = [o for o in objects if isinstance(o["value"], AnalysisStep) and o["value"].model.name == model]
    names = stepwise.model_steps([o["value"] for o in steps]).get(model, [stepwise.INITIAL])
    built_names = {stepwise.INITIAL: stepwise.INITIAL}
    for o in steps:
        built_names.setdefault(o["value"].name, o["name"])
    return names, built_names


def condition_plan(value: Condition, objects: list[dict]) -> list[tuple]:
    """(CAE step name, "create" / "change" / "off", values, copy) for each step where the condition changes."""
    keys = CONDITION_SETTINGS[value.kind]
    base = dict(zip(keys, value.values))
    base["amplitude"] = value.amplitude
    start = value.step.name
    if value.kind in ("Temperature", "Initial velocity") and not value.active:
        start = stepwise.INITIAL
    if not value.stepwise:
        step_name = stepwise.INITIAL if start == stepwise.INITIAL else _condition_step_name(value.step, objects)
        return [(step_name, "create", base, 0)]
    names, built_names = model_step_names(value.step.model.name, objects)
    if start not in names:
        names.append(start)
    events = stepwise.plan(names, start, value.active, base, value.changes, value.name, value.kind in IN_INITIAL)
    if value.kind in ZERO_IS_OFF:
        events = _zero_is_off(events, value.kind)
    if value.kind == "Bolt load" and value.values[1]:
        events = _bolt_fix_steps(events, names)
    return [(built_names.get(e[0], e[0]),) + e[1:] for e in events]


# loads whose values can't all be 0 in CAE ("Load must be created with a non-zero magnitude"), also in a later step
ZERO_IS_OFF = {"Concentrated force": ("x", "y", "z"), "Gravity": ("x", "y", "z"), "Pressure": ("magnitude",),
               "Bolt load": ("force",), "Heat flux": ("flux",)}


def _zero_is_off(events: list[tuple], kind: str) -> list[tuple]:
    """A load set to 0 in a step is removed there (as Abaqus ramps a deactivated load down), and comes back as a
    copy when it is non-zero again."""
    keys = ZERO_IS_OFF[kind]
    out, current, on, copies = [], {}, False, 0
    for e in events:
        step, what = e[0], e[1]
        if what == "create":
            current = dict(e[2])
        elif what == "change":
            current.update(e[2])
        zero = not any(current.get(k) for k in keys)
        if what == "off" or zero:
            if on:
                out.append((step, "off", None))
            on = False
        elif what == "create" or not on:
            out.append((step, "create", dict(current), copies))
            copies, on = copies + 1, True
        else:
            out.append(e)
    return out


def _bolt_fix_steps(events: list[tuple], names: list[str]) -> list[tuple]:
    """A bolt fixed at its length: in the step after each one that applies a force (unless it gives another),
    Fix length ({"__fix": True}); it then holds in the later steps."""
    by_step = {e[0]: e for e in events}
    out, applied, on = [], False, False
    for name in names:
        e = by_step.get(name)
        if e is not None and e[1] == "create":
            out.append(e)
            applied, on = True, True
        elif e is not None and e[1] == "off":
            out.append(e)
            applied, on = False, False
        elif e is not None and "force" in e[2]:
            out.append(e)
            applied = True
        elif on and applied:
            out.append((name, "change", {**(e[2] if e is not None else {}), "__fix": True}))
            applied = False
        elif e is not None:
            out.append(e)
    return out


CONDITION_ARGS = {"Concentrated force": ("cf1", "cf2", "cf3"), "Gravity": ("comp1", "comp2", "comp3")}


def _condition_args(kind: str, values: dict, initial: bool = False, later: bool = False) -> str:
    """The keyword arguments for these settings' values (only those given: a change gives the changed ones)."""
    args = []
    if kind == "Displacement / rotation":
        for k in ("u1", "u2", "u3", "ur1", "ur2", "ur3"):
            if k not in values:
                continue
            v = values[k]
            if initial and v not in (None, 0.0):
                raise ValueError(f"In the Initial step a displacement can only be held at 0 ({k.upper()} is {v:g}): "
                                 "give the value in an analysis step")
            args.append(f"{k}={('FREED' if later else 'UNSET') if v is None else 'SET' if initial else _n(v)}")
    elif kind in CONDITION_ARGS:
        args += [f"{a}={_n(values[k])}" for a, k in zip(CONDITION_ARGS[kind], ("x", "y", "z")) if k in values]
    elif kind in ("Pressure", "Bolt load") and ("magnitude" in values or "force" in values):
        args.append(f"magnitude={_n(values.get('magnitude', values.get('force')))}")
    elif kind == "Temperature" and "temperature" in values:
        args.append(f"magnitudes=({_n(values['temperature'])},)")
    elif kind == "Fixed temperature" and "temperature" in values:
        args.append(f"magnitude={_n(values['temperature'])}")
    elif kind == "Heat flux" and "flux" in values:
        args.append(f"magnitude={_n(values['flux'])}")
    elif kind == "Convection":
        args += [f"{a}={_n(values[k])}" for a, k in (("filmCoeff", "film"), ("sinkTemperature", "sink")) if k in values]
    return ", ".join(args)


AMPLITUDE_MAKERS = {"Tabular": "TabularAmplitude", "Smooth step": "SmoothStepAmplitude",
                    "Equally spaced": "EquallySpacedAmplitude", "Periodic": "PeriodicAmplitude",
                    "Modulated": "ModulatedAmplitude", "Exponential decay": "DecayAmplitude",
                    "Solution dependent": "SolutionDependentAmplitude", "User": "UserAmplitude"}


def _amplitude_lines(name: str, data: tuple | Amplitude, shape: str) -> list[str]:
    """Code making amplitude `name`: typed time, factor pairs in this shape, or an Amplitude node's (any type)."""
    amp = data if isinstance(data, Amplitude) else Amplitude(shape, data)
    args = [f"name={name!r}", f"timeSpan={'TOTAL' if amp.time_span == 'Total' else 'STEP'}"]
    if amp.kind in ("Tabular", "Equally spaced"):
        args.append(f"smooth={'SOLVER_DEFAULT' if amp.smooth is None else _n(amp.smooth)}")
    for key, v in amp.settings:
        args.append(f"{key}={int(v)}" if key == "numVariables" else f"{key}={_n(v)}")
    if amp.kind == "Equally spaced":
        args.append(f"data=({', '.join(_n(a) for a in amp.data)},)")
    elif amp.data:
        args.append(f"data=({', '.join(_pt(row) for row in amp.data)},)")
    make = f"m.{AMPLITUDE_MAKERS[amp.kind]}({', '.join(args)})"
    return [f"if {name!r} in m.amplitudes.keys():", f"    del m.amplitudes[{name!r}]", make]


def _over_time_and_space(value: Condition, name: str) -> tuple[list[str], str, str]:
    """Code for the condition's own amplitude (<name>-Amp) and analytical field (<name>-Field), and the arguments
    that use them: (lines, amplitude argument, field name or '')."""
    lines, amp_arg, field = [], "", ""
    if value.amplitude:
        amp = name + "-Amp"
        lines += _amplitude_lines(amp, value.amplitude, value.amplitude_shape)
        amp_arg = f", amplitude={amp!r}"
    if value.distribution:
        field = name + "-Field"
        lines += [f"if {field!r} in m.analyticalFields.keys():", f"    del m.analyticalFields[{field!r}]"]
        if value.distribution[0] == "expression":
            lines.append(f"m.ExpressionField(name={field!r}, localCsys=None, description='', "
                         f"expression={value.distribution[1]!r})")
        else:
            data = ", ".join("(" + ", ".join(_n(c) for c in row) + ")" for row in value.distribution[1])
            lines.append(f"m.MappedField(name={field!r}, description='', regionType=POINT, partLevelData=False, "
                         f"localCsys=None, pointDataFormat=XYZ, fieldDataType=SCALAR, xyzPointData=({data},))")
    return lines, amp_arg, field


def condition_code(value: Condition, name: str, step_name: str, objects: list[dict]) -> list[str]:
    try:
        events = condition_plan(value, objects)
    except ValueError as exc:  # the editor shows it on the node; CAE says why it wasn't built
        return [f"raise ValueError({str(exc)!r})"]
    lines = [f"m = mdb.models[{value.step.model.name!r}]"]
    extra, _, field = _over_time_and_space(value, name)
    lines += extra
    if value.csys is not None:  # an assembly coordinate system Datum, by the name it was built with
        datum = next((o["name"] for o in objects if o["value"] == value.csys), value.csys.name)
        lines.append(f"_csys = m.rootAssembly.datums[m.rootAssembly.features[{datum!r}].id]")
    if value.kind == "Convection":
        lines.append("import interaction")
    if value.kind == "Gravity":
        pass
    elif isinstance(value.region, Interaction):
        lines.append(f"target_region = {coupling_point(value.region, objects)}")
    elif value.kind == "Bolt load":
        lines += _bolt_axis(value, name, objects)
    else:
        repo = REPOS[value.region.kind]
        # regionToolset rejects a plain tuple: _region_kw rebuilds CAE's own sequence (on the assembly)
        sides = "Edges" if value.region.kind == "edge" else "Faces"  # a 2D part's pressure goes on its edges
        region_arg = (("side1" if value.region.side == "Side 1" else "side2") + sides
                      if value.kind in ("Pressure", "Heat flux", "Convection") else repo)
        lines += ["import regionToolset",
                  f"target_region = regionToolset.Region(**{region_kw_code(value.region, objects, repo, region_arg)})"]
    amplitudes = {value.amplitude: name + "-Amp"} if value.amplitude else {}

    def amplitude(data: tuple, step: str) -> str:
        """The amplitude argument for this table (made the first time it's needed), or Abaqus' default."""
        if not data:
            return "FREED"
        if data not in amplitudes:
            amplitudes[data] = f"{name}-Amp-{step}"
            lines.extend(_amplitude_lines(amplitudes[data], data, value.amplitude_shape))
        return repr(amplitudes[data])

    try:
        current = name
        for event in events:
            step, what = event[0], event[1]
            initial = step == stepwise.INITIAL
            target = f"m.{_condition_repo(value)}[{current!r}]"
            if what == "off":
                lines.append(f"{target}.resetToInitial(stepName={step!r})" if value.kind == "Temperature" else
                             f"{target}.deactivate(stepName={step!r})")
                continue
            if what == "create":
                values, copy = event[2], event[3]
                current = stepwise.copy_name(name, step, copy)
                amp = amplitude(values.get("amplitude", ()), step)
                amp_arg = "" if initial or amp == "FREED" else f", amplitude={amp}"
                lines += _condition_create(value, current, step, values, amp_arg, field, initial)
                if value.kind == "Bolt load" and copy == 0:
                    lines += _bolt_output(name, step)
                continue
            changed = event[2]
            args = _condition_args(value.kind, changed, later=True)
            if value.kind == "Bolt load" and "force" in changed:
                args += ", boltMethod=APPLY_FORCE"
            elif changed.get("__fix"):
                args += (", " if args else "") + "boltMethod=FIX_LENGTH"
            if "amplitude" in changed:
                args += (", " if args else "") + f"amplitude={amplitude(changed['amplitude'], step)}"
            if args:
                lines.append(f"{target}.setValuesInStep(stepName={step!r}, {args})")
        if value.kind == "Bolt load" and value.values[1] and not value.stepwise:
            # fixed at the length the preload gave it in every later step (as CAE's Bolt load editor does)
            lines += ["_names = list(m.steps.keys())",
                      f"for _later in _names[_names.index({events[0][0]!r}) + 1:]:",
                      f"    m.loads[{name!r}].setValuesInStep(stepName=_later, boltMethod=FIX_LENGTH)"]
    except ValueError as exc:
        return [f"raise ValueError({str(exc)!r})"]
    return lines


BC_KINDS = ("Fixed", "Displacement / rotation", "Fixed temperature")


def _condition_repo(value: Condition) -> str:
    return ("predefinedFields" if value.predefined else "interactions" if value.kind == "Convection" else
            "boundaryConditions" if value.kind in BC_KINDS else "loads")


FIX_MAKERS = {"": "EncastreBC", "PINNED": "PinnedBC", "XSYMM": "XsymmBC", "YSYMM": "YsymmBC", "ZSYMM": "ZsymmBC",
              "XASYMM": "XasymmBC", "YASYMM": "YasymmBC", "ZASYMM": "ZasymmBC"}


def _bolt_axis(value: Condition, name: str, objects: list[dict]) -> list[str]:
    """A bolt load's face and its axis along the face's normal (an assembly datum axis named <name>-Axis)."""
    selected, _ = selection_code(value.region, objects, "")
    axis = name + "-Axis"
    return ["import regionToolset", "a = m.rootAssembly",
            f"_face = _geometry({selected}, 'Bolt load face')[0]",
            f"target_region = regionToolset.Region(**{region_kw_code(value.region, objects, 'faces', 'side1Faces')})",
            "_p, _nv = _face.pointOn[0], _face.getNormal()",
            f"if {axis!r} in a.features.keys():", f"    a.deleteFeatures(({axis!r},))",
            "_axis = a.DatumAxisByTwoPoint(point1=_p, point2=(_p[0] + _nv[0], _p[1] + _nv[1], _p[2] + _nv[2]))",
            f"a.features.changeKey(fromName=_axis.name, toName={axis!r})"]


def _bolt_output(name: str, step_name: str) -> list[str]:
    """The bolt's force over time (TF1 at its pretension node), for History Output in Results."""
    return ["import step", f"if {name + '-Force'!r} in m.historyOutputRequests.keys():",
            f"    del m.historyOutputRequests[{name + '-Force'!r}]",
            f"m.HistoryOutputRequest(name={name + '-Force'!r}, createStepName={step_name!r}, variables=('TF',), "
            f"boltLoad={name!r})"]


def _condition_create(value: Condition, name: str, step_name: str, values: dict, amp_arg: str, field: str,
                      initial: bool) -> list[str]:
    kind = value.kind
    over = f"distributionType=FIELD, field={field!r}" if field else "distributionType=UNIFORM, field=''"
    csys = ", localCsys=_csys" if value.csys is not None else ""
    if kind == "Fixed":
        return [f"m.{FIX_MAKERS[value.fix]}(name={name!r}, createStepName={step_name!r}, region=target_region{csys})"]
    if kind == "Displacement / rotation":
        over = f", distributionType=FIELD, fieldName={field!r}" if field and not initial else ""
        return [f"m.DisplacementBC(name={name!r}, createStepName={step_name!r}, region=target_region, "
                f"{_condition_args(kind, values, initial)}{over}{amp_arg}{csys})"]
    if kind == "Fixed temperature":
        return [f"m.TemperatureBC(name={name!r}, createStepName={step_name!r}, region=target_region, "
                f"{_condition_args(kind, values)}, distributionType=UNIFORM, fieldName=''{amp_arg})"]
    if kind == "Heat flux":
        return [f"m.SurfaceHeatFlux(name={name!r}, createStepName={step_name!r}, region=target_region, "
                f"{_condition_args(kind, values)}{amp_arg})"]
    if kind == "Convection":
        return [f"m.FilmCondition(name={name!r}, createStepName={step_name!r}, surface=target_region, "
                f"definition=EMBEDDED_COEFF, {_condition_args(kind, values)}, filmCoeffAmplitude='', "
                "sinkAmplitude='', sinkDistributionType=UNIFORM, sinkFieldName='')"]
    if kind == "Gravity":
        return [f"m.Gravity(name={name!r}, createStepName={step_name!r}, {_condition_args(kind, values)}, "
                f"distributionType=UNIFORM, field=''{amp_arg})"]
    if kind == "Concentrated force":
        return [f"m.ConcentratedForce(name={name!r}, createStepName={step_name!r}, region=target_region, "
                f"{_condition_args(kind, values)}, {over}, localCsys={'_csys' if value.csys is not None else 'None'}"
                f"{amp_arg})"]
    if kind == "Bolt load":
        return [f"m.BoltLoad(name={name!r}, createStepName={step_name!r}, region=target_region, "
                f"{_condition_args(kind, values)}, boltMethod=APPLY_FORCE, datumAxis=a.datums[_axis.id]{amp_arg})"]
    if kind == "Temperature":
        return [f"m.Temperature(name={name!r}, createStepName={step_name!r}, region=target_region, {over}, "
                f"crossSectionDistribution=CONSTANT_THROUGH_THICKNESS, {_condition_args(kind, values)}{amp_arg})"]
    if kind == "Initial velocity":
        return [f"m.Velocity(name={name!r}, region=target_region, field='', distributionType=MAGNITUDE, "
                f"velocity1={_n(values['x'])}, velocity2={_n(values['y'])}, velocity3={_n(values['z'])}, omega=0.0)"]
    return [f"m.Pressure(name={name!r}, createStepName={step_name!r}, region=target_region, "
            f"{over}, {_condition_args(kind, values)}{amp_arg})"]
