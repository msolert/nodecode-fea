"""Step: analysis procedures and their output requests."""

from __future__ import annotations

import re

from ...values import AnalysisStep, Instance, Interaction, Model, OutputRequests, Region
from ..core import Param, Port, register
from ..common import _finite, _region_model
from .geometry import _abaqus_name, DEFAULT_MODEL, _graph_model, _model_value_display, _numbered


def _output_names(text: str, label: str) -> tuple:
    names = tuple(n.upper() for n in re.split(r"[\s,;]+", str(text or "").strip()) if n)
    bad = [n for n in names if not re.fullmatch(r"[A-Z][A-Z0-9_]*", n)]
    if bad:
        raise ValueError(f"{label}: '{bad[0]}' isn't an Abaqus output variable name (like S, U, RF2, PEEQ)")
    return names


STEP_KINDS = ["Static", "Static Riks", "Dynamic implicit", "Dynamic explicit", "Frequency", "Buckling",
              "Heat transfer", "Coupled temperature-displacement"]
THERMAL_STEPS = ["Heat transfer", "Coupled temperature-displacement"]
TIMED_STEPS = ["Static", "Dynamic implicit", "Dynamic explicit"] + THERMAL_STEPS
INCREMENTED = ["Static", "Static Riks", "Dynamic implicit"] + THERMAL_STEPS
EIGEN_STEPS = ["Frequency", "Buckling"]


@register("model.step", "Step", "Model",
          "Create an Abaqus analysis step: Static, Static Riks (arc length: snap-through, post-buckling, collapse "
          "load), Dynamic implicit, Dynamic explicit (impact, crash, drop test, short events; with mass scaling), "
          "Frequency (natural frequencies, modes), Buckling (linear buckling: critical load factors, eigenvalues), "
          "Heat transfer (thermal, conduction, steady state or transient) or Coupled temperature-displacement "
          "(thermal stress in one step). Feed a previous Step to chain procedures; feed a Model or "
          "leave empty for its first step. Lists of settings create parameter-study "
          "step descriptions. Output requests: the field variables written each frame (fewer for a smaller ODB, "
          "more such as PEEQ or LE), how many frames, and history output (e.g. RF2, U2) at a History region, or "
          "energies (ALLSE, ALLWK) for the whole model.",
          inputs=[Port("after", "any", "Model or previous step", optional=True, depth=0),
                  Port("history_region", "any", "History region", optional=True, depth=0,
                       help="Where History outputs are recorded (a Region on an Instance, e.g. the fixed face for "
                            "reaction forces, a coupling's point, or a connector for CTF, CU). Not wired: the whole "
                            "model.")],
          outputs=[Port("step", "analysis_step", "Step")],
          params=[Param("name", "text", "Step-1", "Name"),
                  Param("kind", "select", "Static", "Procedure",
                        options=STEP_KINDS),
                  Param("time", "number", 1.0, "Time period", when={"kind": TIMED_STEPS + ["Static Riks"]},
                        help="Static Riks: the total arc length the increments add up to (load factor scale)."),
                  Param("nlgeom", "select", "No", "Geometric nonlinearity", options=["No", "Yes"],
                        when={"kind": [k for k in TIMED_STEPS if k != "Heat transfer"] + ["Static Riks"]},
                        help="Static Riks always follows large displacements (Yes)."),
                  Param("response", "select", "Steady state", "Response", options=["Steady state", "Transient"],
                        when={"kind": THERMAL_STEPS},
                        help="Steady state: the final temperatures (no specific heat needed). Transient: how they "
                             "change over the time period (needs density and specific heat)."),
                  Param("max_temperature_change", "number", 0.0, "Max temperature change",
                        when={"kind": THERMAL_STEPS, "response": ["Transient"]},
                        help="Largest temperature change allowed in an increment (automatic increments). 0: fixed "
                             "increments of the initial size."),
                  Param("initial", "number", 0.1, "Initial increment", when={"kind": INCREMENTED},
                        help="Static Riks: the initial arc length increment."),
                  Param("maximum", "number", 1.0, "Maximum increment", when={"kind": INCREMENTED}),
                  Param("minimum", "number", 1e-5, "Minimum increment", when={"kind": INCREMENTED}),
                  Param("increments", "int", 100, "Maximum increments", when={"kind": INCREMENTED}),
                  Param("max_load_factor", "number", 0.0, "Maximum load factor", when={"kind": ["Static Riks"]},
                        help="Stop once the loads reach this multiple of their values (load proportionality "
                             "factor LPF). 0: run the Maximum increments."),
                  Param("scale", "number", 1.0, "Time scaling factor", when={"kind": ["Dynamic explicit"]},
                        help="Multiplies Abaqus/Explicit's stable time increment (below 1 for a safer, smaller one)."),
                  Param("mass_dt", "number", 0.0, "Mass scaling increment", when={"kind": ["Dynamic explicit"]},
                        help="Target stable time increment: elements whose increment is smaller get extra mass to "
                             "reach it (faster runs, slightly more inertia). 0: no mass scaling."),
                  Param("modes", "int", 10, "Eigenmodes", when={"kind": EIGEN_STEPS},
                        help="How many eigenvalues: natural frequencies (Frequency) or buckling load factors "
                             "(Buckling: the step's loads times each factor buckle the structure)."),
                  Param("field", "text", "", "Field outputs",
                        help="Variables written each frame, e.g. S, U, RF, PEEQ (ALL for everything). Empty: "
                             "Abaqus' preselected defaults."),
                  Param("intervals", "int", 0, "Output frames",
                        help="Write field output at this many evenly spaced times. 0: every increment."),
                  Param("history", "text", "", "History outputs",
                        help="Variables recorded every increment, e.g. RF2, U2 at the History region, or ALLSE, "
                             "ALLWK for the whole model. Empty: Abaqus' preselected energies only.")],
          display=lambda o, i, p: _model_value_display(o["step"]))
def model_step(inputs, params):
    after = inputs.get("after")
    if after is None:
        model, previous = _graph_model(inputs, Model(DEFAULT_MODEL)), None
    elif isinstance(after, Model):
        model, previous = after, None
    elif isinstance(after, AnalysisStep):
        model, previous = after.model, after
    else:
        raise ValueError("Step's input takes a Model or previous Step")
    _finite(params, {"time": "Time period", "initial": "Initial increment", "maximum": "Maximum increment",
                     "minimum": "Minimum increment"})
    kind = str(params["kind"])
    if isinstance(after, AnalysisStep) and (kind == "Dynamic explicit") != (after.kind == "Dynamic explicit"):
        raise ValueError("Abaqus/Explicit and Abaqus/Standard steps can't follow each other in one model: chain "
                         "Dynamic explicit steps only after the Model or other Dynamic explicit steps")
    if isinstance(after, AnalysisStep) and ((kind in EIGEN_STEPS and after.kind not in ("Static", "Static Riks",
                                                                         *EIGEN_STEPS)) or
                                            (kind not in EIGEN_STEPS and after.perturbation)):
        raise ValueError(f"{kind if kind in EIGEN_STEPS else after.kind} is a perturbation step: connect it after a "
                         "Static step (or the Model), not directly before/after another procedure")
    if isinstance(after, AnalysisStep) and (kind == "Heat transfer") != (after.kind == "Heat transfer"):
        raise ValueError("A heat transfer analysis has only heat transfer steps (its elements have temperatures, no "
                         "displacements): chain Heat transfer after the Model or another Heat transfer step, or use "
                         "Coupled temperature-displacement for thermal stress")
    time = float(params["time"])
    initial, maximum, minimum = float(params["initial"]), float(params["maximum"]), float(params["minimum"])
    increments, modes = int(params["increments"]), int(params["modes"])
    if kind not in EIGEN_STEPS and (time <= 0 or initial <= 0 or maximum <= 0 or minimum <= 0 or increments < 1):
        raise ValueError("Time and increments must be positive")
    if kind in INCREMENTED and not minimum <= initial <= maximum:
        raise ValueError("Increments go Minimum ≤ Initial ≤ Maximum")
    if kind in EIGEN_STEPS and modes < 1:
        raise ValueError("Eigenmodes must be positive")
    transient = kind in THERMAL_STEPS and params.get("response") == "Transient"
    change = float(params.get("max_temperature_change") or 0.0) if transient else 0.0
    lpf = float(params.get("max_load_factor") or 0.0) if kind == "Static Riks" else 0.0
    _finite({"change": change, "lpf": lpf}, {"change": "Max temperature change", "lpf": "Maximum load factor"})
    if change < 0 or lpf < 0:
        raise ValueError("Max temperature change and Maximum load factor are 0 (none) or positive")
    scale = float(params["scale"] if params.get("scale") not in (None, "") else 1.0)
    mass_dt = float(params.get("mass_dt") or 0.0)
    if kind == "Dynamic explicit":
        _finite({"scale": scale, "mass_dt": mass_dt}, {"scale": "Time scaling factor",
                                                      "mass_dt": "Mass scaling increment"})
        if scale <= 0 or mass_dt < 0:
            raise ValueError("Time scaling factor must be positive and the mass scaling increment 0 (none) or more")
    else:
        scale, mass_dt = 1.0, 0.0
    name = _numbered(_abaqus_name(str(params["name"]), "Step-1"), params, model=model)
    nlgeom = kind == "Static Riks" or (params["nlgeom"] == "Yes" and kind != "Heat transfer")
    step = AnalysisStep(name, model, kind, previous, time, nlgeom,
                        initial, maximum, minimum, increments, modes, scale=scale, mass_dt=mass_dt,
                        transient=transient, max_temperature_change=change, max_load_factor=lpf)
    field = _output_names(params.get("field"), "Field outputs")
    history = _output_names(params.get("history"), "History outputs")
    intervals = int(params.get("intervals") or 0)
    if intervals < 0:
        raise ValueError("Output frames can't be negative (0: every increment)")
    region = inputs.get("history_region")
    if isinstance(region, Interaction) and (region.point_owner or region.kind == "Connector"):
        if region.model != model:
            raise ValueError("The History region's coupling belongs to another model")
        if not history:
            raise ValueError("History region is wired: list the History outputs to record there (e.g. RF2, U2)")
    elif region is not None:
        if not isinstance(region, Region):
            raise ValueError("History region takes a Region on an Instance or a coupling (its reference point)")
        if not isinstance(region.source, Instance) and region.kind != "reference point":  # a part's RP: every instance's
            raise ValueError("History region needs a Region on an Instance (the assembly), not on a Part")
        if _region_model(region) != model:
            raise ValueError("The History region belongs to another model")
        if not history:
            raise ValueError("History region is wired: list the History outputs to record there (e.g. RF2, U2)")
    if not (field or intervals or history):
        return {"step": step}
    return {"step": step, "requests": OutputRequests(step, field, intervals, history, region)}
