"""Analysis steps and output requests."""

from __future__ import annotations

from ..values import AnalysisStep, Interaction, OutputRequests
from .core import _n
from .regions import coupling_point, region_kw_code, REPOS


def step_code(value: AnalysisStep, name: str, previous: str) -> list[str]:
    lines = [f"m = mdb.models[{value.model.name!r}]"]
    increments = (f"initialInc={_n(value.initial)}, maxInc={_n(value.maximum)}, minInc={_n(value.minimum)}, "
                  f"maxNumInc={int(value.increments)}")
    if value.kind == "Frequency":
        lines.append(f"m.FrequencyStep(name={name!r}, previous={previous!r}, numEigen={int(value.modes)})")
    elif value.kind == "Buckling":
        # subspace iteration needs more vectors than eigenvalues; CAE's default is too few for one mode
        lines.append(f"m.BuckleStep(name={name!r}, previous={previous!r}, numEigen={int(value.modes)}, "
                     f"vectors={max(2 * int(value.modes), int(value.modes) + 8)}, maxIterations=300)")
    elif value.kind == "Static Riks":
        lpf = f", maxLPF={_n(value.max_load_factor)}" if value.max_load_factor else ""
        lines.append(f"m.StaticRiksStep(name={name!r}, previous={previous!r}, nlgeom=ON, "
                     f"initialArcInc={_n(value.initial)}, maxArcInc={_n(value.maximum)}, "
                     f"minArcInc={_n(value.minimum)}, maxNumInc={int(value.increments)}, "
                     f"totalArcLength={_n(value.time)}{lpf})")
    elif value.thermal:
        response = "TRANSIENT" if value.transient else "STEADY_STATE"
        change = f", deltmx={_n(value.max_temperature_change)}" if value.max_temperature_change else ""
        if value.kind == "Heat transfer":
            lines.append(f"m.HeatTransferStep(name={name!r}, previous={previous!r}, response={response}, "
                         f"timePeriod={_n(value.time)}, {increments}{change})")
        else:
            lines.append(f"m.CoupledTempDisplacementStep(name={name!r}, previous={previous!r}, response={response}, "
                         f"timePeriod={_n(value.time)}, nlgeom={'ON' if value.nlgeom else 'OFF'}, "
                         f"{increments}{change})")
    elif value.explicit:
        scaling = ""
        if value.mass_dt > 0:  # whole model, at the step's start, elements whose stable increment is below dt
            scaling = (f", massScaling=((SEMI_AUTOMATIC, MODEL, AT_BEGINNING, 0.0, {_n(value.mass_dt)}, BELOW_MIN, "
                       "0, 0, 0.0, 0.0, 0, None),)")
        lines.append(f"m.ExplicitDynamicsStep(name={name!r}, previous={previous!r}, timePeriod={_n(value.time)}, "
                     f"nlgeom={'ON' if value.nlgeom else 'OFF'}, scaleFactor={_n(value.scale)}{scaling})")
    else:
        method = "StaticStep" if value.kind == "Static" else "ImplicitDynamicsStep"
        lines.append(f"m.{method}(name={name!r}, previous={previous!r}, timePeriod={_n(value.time)}, "
                     f"nlgeom={'ON' if value.nlgeom else 'OFF'}, initialInc={_n(value.initial)}, "
                     f"maxInc={_n(value.maximum)}, minInc={_n(value.minimum)}, maxNumInc={int(value.increments)})")
    return lines


def output_code(value: OutputRequests, step_name: str, objects: list[dict]) -> list[str]:
    """A step's output requests: field output through the default F-Output-1 in this step (its own request when that
    one is gone or can't change here), history output as its own request, at a region or for the whole model."""
    # output requests appear on models once the step module is loaded (CAE may not have opened it yet)
    lines = ["import step", f"m = mdb.models[{value.step.model.name!r}]"]
    if value.field or value.intervals:
        variables = ("PRESELECT" if not value.field else "ALL" if value.field == ("ALL",) else repr(value.field))
        when = f"numIntervals={int(value.intervals)}" if value.intervals else "frequency=1"
        lines += ["_done = False", "if 'F-Output-1' in m.fieldOutputRequests.keys():", "    try:",
                  f"        m.fieldOutputRequests['F-Output-1'].setValuesInStep(stepName={step_name!r}, "
                  f"variables={variables}, {when})", "        _done = True", "    except Exception:", "        pass",
                  "if not _done:",
                  f"    m.FieldOutputRequest(name={step_name + '-Field'!r}, createStepName={step_name!r}, "
                  f"variables={variables}, {when})"]
    if value.history:
        variables = "ALL" if value.history == ("ALL",) else repr(value.history)
        where = ""
        if isinstance(value.region, Interaction):
            lines.append(f"history_region = {coupling_point(value.region, objects)}")
            where = ", region=history_region"
        elif value.region is not None:
            repo = REPOS[value.region.kind]
            lines += ["import regionToolset",
                      f"history_region = regionToolset.Region(**{region_kw_code(value.region, objects, repo, repo)})"]
            where = ", region=history_region"
        lines.append(f"m.HistoryOutputRequest(name={step_name + '-History'!r}, createStepName={step_name!r}, "
                     f"variables={variables}, frequency=1{where})")
    return lines
