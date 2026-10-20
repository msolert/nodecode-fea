"""Model checks: what Abaqus would refuse or get wrong, found in the graph as it is edited and put on the node that
causes it (``results[node]["warnings"]``), instead of in a .dat file after the job aborts.

Instant checks read the modelling values only (no CAE needed):

- an Instance whose part has no Material & Section, or no mesh;
- an Instance that nothing holds: no boundary condition on it, nor on what it is tied, coupled, connected or in
  contact with (Abaqus stops with a zero pivot / numerical singularity), unless every step is a Frequency step;
- a material without density in a model with a Dynamic or Frequency step or Gravity;
- Young's modulus and density that aren't in one unit system, and Gravity that doesn't match the units;
- a Sketch that draws the same item twice (a pattern of angles from 0 to 360° inclusive);
- a Job of a model with no Step or an empty assembly.

Abaqus' own data check (the Job's Check button, ``abaqus datacheck``) adds its messages from the check's .dat and
.msg files: each goes to the node that made the object it names (a material, a load, an instance…), or to the
Job when it names nothing the graph made.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

from . import jobs
from .cae_model import _core, _flat, final_part
from .values import Amplitude, AnalysisStep, Condition, Instance, Interaction, Job, MergedPart, Part, Region, Sketch

HOLDING = ("Fixed", "Displacement / rotation")
# element types Abaqus/Explicit lacks (checked with Abaqus 2025) and what to use there instead
NOT_EXPLICIT = {"C3D20R": "C3D8R or C3D10", "C3D8H": "C3D8R (Explicit needs no hybrid elements)",
                "C3D10H": "C3D10", "S8R": "S4R"}
SI_WAVE = (300.0, 3.0e4)  # m/s: metals, polymers, concrete…
MM_WAVE = (3.0e5, 3.0e7)  # mm/s


def _units(young: float, density: float = 0.0) -> str:
    """'mm' (N, MPa, tonne/mm³), 'SI' (N, Pa, kg/m³) or '' when the values fit neither."""
    if density > 0:
        c = math.sqrt(young / density)
        return "SI" if SI_WAVE[0] <= c <= SI_WAVE[1] else "mm" if MM_WAVE[0] <= c <= MM_WAVE[1] else ""
    return "SI" if young > 1e8 else "mm" if 1e2 < young < 1e7 else ""


def _thickness(part: Part) -> float:
    """The thinnest overall size of an extruded solid: its depth, or its profile's width or height (0: unknown)."""
    if part.method != "Extrude" or part.sketch is None or part.depth <= 0:
        return 0.0
    points = [p for line in part.sketch.outline() for p in line]
    if not points:
        return 0.0
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(part.depth, max(xs) - min(xs), max(ys) - min(ys))


def _rounded(x):
    """An item with its numbers rounded to 1e-6, so a point at 360° lands on the one at 0°."""
    if isinstance(x, float):
        return round(x, 6) + 0.0  # -0.0 -> 0.0
    if isinstance(x, (tuple, list)):
        return tuple(_rounded(c) for c in x)
    return x


def _repeated_items(items) -> list[tuple[int, int]]:
    """(first, again) indices of the items drawn more than once."""
    seen: dict = {}
    out = []
    for i, it in enumerate(items):
        key = _rounded(it)
        if key in seen:
            out.append((seen[key], i))
        else:
            seen[key] = i
    return out


def _item_text(it) -> str:
    kind = it[0].lower()
    if it[0] in ("Circle", "Arc", "Polygon"):
        return f"{kind} (centre {it[1][0]:g}, {it[1][1]:g}, radius {it[2]:g})"
    if it[0] in ("Ellipse", "Slot"):
        return f"{kind} (centre {it[1][0]:g}, {it[1][1]:g})"
    if it[0] == "Rectangle":
        return f"rectangle ({it[1][0]:g}, {it[1][1]:g} to {it[2][0]:g}, {it[2][1]:g})"
    return kind


def lint(graph: dict, results: dict, node_inputs) -> dict[str, list[str]]:
    """{node id: [warning, …]} for the graph's modelling."""
    outs = getattr(node_inputs, "outputs", {})
    types = {n["id"]: n.get("type") for n in graph.get("nodes", [])}
    entries = [(nid, v) for nid, out in outs.items() if results.get(nid, {}).get("status") == "ok"
               for v in _flat(list(out.values()))]
    warn: dict[str, list[str]] = defaultdict(list)

    def add(nid: str, text: str):
        if text not in warn[nid]:
            warn[nid].append(text)

    parts = [v for _, v in entries if isinstance(v, Part)]
    steps = [(nid, v) for nid, v in entries if isinstance(v, AnalysisStep)]
    conditions = [(nid, v) for nid, v in entries if isinstance(v, Condition)]
    interactions = [v for _, v in entries if isinstance(v, Interaction)]
    analysed = {v.model.name for _, v in steps} | {v.model.name for _, v in entries if isinstance(v, Job)}
    subroutines = {v.model.name for _, v in entries if isinstance(v, Job) and v.user}

    # instances: the last placement of each (a chain of Instance nodes is one CAE instance)
    instances: dict[tuple, tuple[str, Instance]] = {}
    for nid, v in entries:
        if isinstance(v, Instance) and types.get(nid) in ("model.instance", "model.merge_instances"):
            key = (v.part.model.name, v.name)
            if key not in instances or len(v.steps) >= len(instances[key][1].steps):
                instances[key] = (nid, v)
    instances = {key: item for key, item in instances.items() if not item[1].removed}

    # --- parts placed for an analysis: section and mesh
    for (model, name), (nid, inst) in instances.items():
        part = final_part(inst.part, parts)
        if model not in analysed or inst.existing or part.existing or isinstance(part, MergedPart):
            continue
        if part.rigid:
            if not any(op.kind == "Reference point" for op in part.operations):
                add(nid, f"Rigid part {part.name} has no reference point: Abaqus needs one to move it (Part Feature "
                         "→ Reference point; hold or load the point it outputs).")
            if part.rigid == "Discrete" and not any(f and f[0] == "Mesh" for f in part.features):
                add(nid, f"Discrete rigid part {part.name} isn't meshed: Add Mesh (R3D4, R2D2 or RAX2 elements).")
            continue
        if not part.sections:
            add(nid, f"Part {part.name} has no Material & Section: Abaqus stops ('elements have missing property "
                     "definitions'). Add Material & Section before Mesh / Instance.")
        if not part.mesh_file and not any(f and f[0] == "Mesh" for f in part.features):
            add(nid, f"Part {part.name} isn't meshed: Abaqus needs elements. Add Mesh before Instance.")

    # --- one modelling space per model (CAE refuses to mix 3D, 2D planar and axisymmetric instances)
    spaces: dict[str, dict[str, str]] = defaultdict(dict)
    for (model, name), (nid, inst) in instances.items():
        spaces[model].setdefault(getattr(inst.part, "space", "3D"), nid)
    for model, found in spaces.items():
        if len(found) > 1:
            for space, nid in found.items():
                add(nid, f"Model {model} mixes {' and '.join(sorted(found))} parts: an assembly is all one "
                         "modelling space (Part's Modelling space).")

    # --- what holds each instance
    def instances_of(region) -> list[tuple]:
        if isinstance(region, Interaction):  # a coupling: its faces
            return instances_of(region.slave) if region.point_owner else []
        if not isinstance(region, Region):
            return []
        if isinstance(region.source, Instance):
            return [(region.source.part.model.name, region.source.name)]
        mine = final_part(region.source, parts)
        return [k for k, (_, inst) in instances.items() if final_part(inst.part, parts) == mine]

    parent = {k: k for k in instances}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def join(keys):
        keys = [k for k in keys if k in parent]
        for k in keys[1:]:
            parent[root(k)] = root(keys[0])

    for v in interactions:
        if v.kind == "General contact" and v.master is None:
            join([k for k in instances if k[0] == v.model.name])
        else:
            join(instances_of(v.master) + instances_of(v.slave))
    held = set()
    for _, c in conditions:
        if c.kind in HOLDING:
            held.update(root(k) for k in instances_of(c.region) if k in parent)
    for model in {k[0] for k in instances}:
        model_steps = [s for _, s in steps if s.model.name == model]
        if not model_steps or all(s.kind == "Frequency" or s.kind == "Heat transfer" for s in model_steps):
            continue  # free-free vibration is a valid analysis
        if any(s.explicit for s in model_steps):
            continue  # Abaqus/Explicit has no stiffness matrix to go singular: a free body just flies (impact)
        for key, (nid, inst) in instances.items():
            if key[0] == model and root(key) not in held and not inst.existing:
                add(nid, f"Nothing holds {inst.name}: no boundary condition on it or on what it is tied, coupled, "
                         "connected or in contact with, so it can move freely (Abaqus stops: zero pivot / "
                         "numerical singularity). Fix a face with Load / Boundary Condition, or tie it to a held "
                         "part.")

    # --- materials: density where it is needed, and one unit system
    needs_mass = {s.model.name for _, s in steps if s.kind in ("Dynamic implicit", "Dynamic explicit", "Frequency")}
    needs_mass |= {c.step.model.name for _, c in conditions if c.kind == "Gravity"}
    systems: dict[str, set] = defaultdict(set)
    for nid, v in entries:
        if not (isinstance(v, Part) and types.get(nid) == "model.section"):
            continue
        for section, _ in v.sections:
            material = section.material
            if not section.known:  # picked or copied from CAE: its properties aren't known here
                continue
            if material.model.name in needs_mass and material.density <= 0:
                add(nid, f"Material {material.name} has no density: a Dynamic or Frequency step and Gravity need "
                         "it (steel: 7.85e-9 tonne/mm³ with E in MPa, 7850 kg/m³ with E in Pa).")
            system = _units(material.young, material.density)
            if material.density > 0 and not system:
                add(nid, f"Material {material.name}: Young's modulus {material.young:g} and density "
                         f"{material.density:g} aren't in one unit system. With E in MPa (mm, N) density is in "
                         "tonne/mm³ (steel 7.85e-9); with E in Pa (m, N), in kg/m³ (steel 7850).")
            if system:
                systems[material.model.name].add(system)
    for nid, c in conditions:
        if c.kind != "Gravity" or len(systems.get(c.step.model.name, ())) != 1:
            continue
        g = math.sqrt(sum(float(x) ** 2 for x in c.values))
        system = next(iter(systems[c.step.model.name]))
        if system == "mm" and 1 < g < 100:
            add(nid, f"Gravity {g:g} looks like m/s², but the materials are in mm, N, MPa: gravity is 9810 mm/s².")
        elif system == "SI" and 1000 < g < 100000:
            add(nid, f"Gravity {g:g} looks like mm/s², but the materials are in m, N, Pa: gravity is 9.81 m/s².")

    explicit = {s.model.name for _, s in steps if s.explicit}
    kinds_in: dict[str, set] = defaultdict(set)
    for _, s in steps:
        kinds_in[s.model.name].add(s.kind)
    # --- heat transfer: thermal properties and elements with temperatures
    for nid, v in entries:
        if not (isinstance(v, Part) and types.get(nid) == "model.section"):
            continue
        kinds = kinds_in.get(v.model.name, set())
        transient = any(s.transient for _, s in steps if s.model.name == v.model.name and s.thermal)
        for section, _ in v.sections:
            material = section.material
            if not section.known or not kinds & {"Heat transfer", "Coupled temperature-displacement"}:
                continue
            if material.conductivity <= 0:
                add(nid, f"Material {material.name} has no conductivity: a heat transfer or coupled step needs it.")
            if transient and (material.specific_heat <= 0 or material.density <= 0):
                add(nid, f"Material {material.name}: a transient heat transfer needs Specific heat and Density.")
    # --- mesh: enough elements through a solid's thickness, element types that mislead
    for nid, v in entries:
        if not (isinstance(v, Part) and types.get(nid) == "model.mesh" and v.features and v.features[-1][0] == "Mesh"):
            continue
        _, selector, size, _, _, _, shape, element, _, seeding = v.features[-1][:10]
        kinds = kinds_in.get(v.model.name, set())
        heat = element.startswith(("DC", "DS"))
        if "Heat transfer" in kinds and not heat:
            add(nid, f"{element} has no temperatures: a Heat transfer step needs heat transfer elements (DC3D8, "
                     "DC3D4, DC2D4, DCAX4, DS4…).")
        elif "Coupled temperature-displacement" in kinds and not element.endswith("T"):
            add(nid, f"{element} has no temperatures: a Coupled temperature-displacement step needs coupled "
                     "elements (C3D8T, C3D8RT, C3D10MT, CPE4T, CAX4T…).")
        elif heat and kinds and "Heat transfer" not in kinds:
            add(nid, f"{element} is a heat transfer element (temperatures only): the model's "
                     f"{', '.join(sorted(kinds))} step needs displacements (C3D8R, CPS4R…).")
        if element in NOT_EXPLICIT and v.model.name in explicit:
            add(nid, f"{element} isn't available in Abaqus/Explicit (the Dynamic explicit step): use "
                     f"{NOT_EXPLICIT[element]}.")
        if element == "C3D4":
            add(nid, "Linear tetrahedra (C3D4) are much too stiff for stresses and bending; use C3D10 (quadratic).")
        rubber = [s.material.name for s, _ in v.sections if s.material.hyper and s.material.hyper[-1] == 0]
        if rubber and v.effective_body == "Solid" and not element.endswith("H"):
            add(nid, f"Material {rubber[0]} is incompressible (D1 = 0): Abaqus needs hybrid elements for it. Choose "
                     "C3D8H or C3D10H, or give D1 > 0 (2 / bulk modulus).")
        thin = _thickness(v)
        if (thin and not selector and not seeding and v.effective_body == "Solid" and element in ("Automatic", "C3D8R")
                and shape != "Tet" and size > thin / 2 + 1e-9):
            across = max(1, round(thin / size))
            add(nid, f"Seed size {size:g} gives about {across} element{'s' if across != 1 else ''} across the "
                     f"{thin:g} thickness: linear reduced-integration hexes (C3D8R) need about 4 to bend right "
                     f"(seed ≤ {thin / 4:g}), or use C3D8I (incompatible modes) or C3D20R.")

    # --- two Mesh nodes seeding the whole of one part (side by side or chained): it is one CAE part, the later wins
    whole: dict[Part, list[str]] = defaultdict(list)
    for nid, v in entries:
        if isinstance(v, Part) and types.get(nid) == "model.mesh" and v.features and v.features[-1][0] == "Mesh":
            mesh = v.features[-1]
            on_edges = len(mesh) > 10 and mesh[10] == "edges"
            if (not mesh[1] or mesh[1][0] != "edge") and not on_edges and nid not in whole[_core(v)]:
                whole[_core(v)].append(nid)
    for part, nids in whole.items():
        for nid in nids if len(nids) > 1 else []:
            add(nid, f"{len(nids)} Mesh nodes seed the whole of {part.name}, which is one part in CAE: the later "
                     "seed replaces the earlier. For a finer area, Mesh a Region of it instead (Edge, or Face/Cell "
                     "with Face/Cell Region seed: Its edges); for separate meshed parts, give a list of sizes or "
                     "another part name.")

    # --- loads and boundary conditions
    youngs = defaultdict(list)
    for _, v in entries:
        if isinstance(v, Part):
            for section, _ in v.sections:
                if section.known:
                    youngs[section.material.model.name].append(section.material.young)
    holding: dict[tuple, list] = defaultdict(list)
    for nid, c in conditions:
        r = c.region
        if c.kind == "Concentrated force" and isinstance(r, Region) and not r.dynamic and len(r.indices or r.points) > 1:
            add(nid, f"The force acts in full at each of the {len(r.indices or r.points)} vertices (total "
                     f"{len(r.indices or r.points)} times the value). For a total force on several points, couple "
                     "them to one point (Interaction: coupling) and load that.")
        if c.kind == "Pressure" and youngs.get(c.step.model.name) and c.values:
            e = max(youngs[c.step.model.name])
            if e > 0 and abs(float(c.values[0])) > 0.05 * e:
                add(nid, f"Pressure {float(c.values[0]):g} is {abs(float(c.values[0])) / e:.0%} of Young's modulus "
                         f"{e:g}: check the units (with E in MPa, pressure is in MPa too).")
        if (c.step.explicit and not c.amplitude and c.kind in ("Displacement / rotation", "Concentrated force",
                                                                "Pressure") and any(x for x in c.values)):
            add(nid, f"Without an amplitude, {c.kind.split(' /')[0].lower()} in the Dynamic explicit step "
                     f"{c.step.name} is applied at once (Explicit's default is a step, not a ramp): a shock wave "
                     "and noisy results. Give it an amplitude (Smooth step 0, 0; "
                     f"{c.step.time:g}, 1) unless an impact is meant.")
        amp = c.amplitude if isinstance(c.amplitude, Amplitude) else Amplitude("Tabular", c.amplitude)
        points = amp.points() if amp.time_span == "Step" else ()
        if points and c.step.kind != "Frequency":
            end = max(t for t, _ in points)
            if end < c.step.time * (1 - 1e-6):
                add(nid, f"The amplitude ends at time {end:g} but the step lasts {c.step.time:g}: Abaqus holds its "
                         f"last value ({points[-1][1]:g}) after it.")
        if amp.kind == "User" and c.step.model.name not in subroutines:
            add(nid, "A User amplitude comes from a UAMP (Standard) or VUAMP (Explicit) subroutine: give the Job "
                     "its User subroutine file, or the analysis stops.")
        if amp.kind == "Solution dependent":
            add(nid, "A Solution dependent amplitude only changes under creep strain rate control (superplastic "
                     "forming, set up in the input file); otherwise it stays at its Initial factor.")
        if c.kind in HOLDING and isinstance(r, Region):
            holding[(c.step.model.name, r)].append((nid, c.name))
    for (_, _), held_by in holding.items():
        names = sorted({name for _, name in held_by})
        if len(names) > 1:
            for nid, name in held_by:
                others = ", ".join(n for n in names if n != name)
                add(nid, f"{others} also holds these faces: two boundary conditions on the same region "
                         "over-constrain it (Abaqus keeps one and warns). Keep one, or use Displacement for both.")
    for v in interactions:
        if isinstance(v.master, Region) and v.master == v.slave:
            for nid, x in entries:
                if x is v:
                    add(nid, f"{v.name}: Master and Slave are the same faces; tie or contact needs two surfaces "
                             "(for self-contact use General contact).")

    # --- sketches: the same item drawn twice (CAE can't make a face or solid from two identical loops)
    for nid, v in entries:
        if isinstance(v, Sketch) and types.get(nid) == "model.sketch":
            for first, again in _repeated_items(v.items):
                add(nid, f"{v.name} draws the same {_item_text(v.items[first])} twice (items {first + 1} and "
                         f"{again + 1}): CAE can't build a part from it ('feature failed'). A pattern of angles "
                         "from 0 to 360° repeats its first item at 360°: stop one step before (e.g. "
                         "np.linspace(0, 360, n, endpoint=False)).")

    # --- jobs
    for nid, v in entries:
        if not isinstance(v, Job):
            continue
        model = v.model.name
        if v.model.file:  # an imported model brings its own steps and assembly
            continue
        if not any(s.model.name == model for _, s in steps):
            add(nid, f"Model {model} has no Step: Abaqus has nothing to solve. Add a Step.")
        if not any(k[0] == model for k in instances):
            add(nid, f"Model {model} has an empty assembly: place its parts with Instance.")
    for nid, text in datacheck_warnings(graph, results, node_inputs, entries, instances).items():
        for t in text:
            add(nid, t)
    # --- how each run converges (or why it stopped)
    for nid, job in [(nid, v) for nid, v in entries if isinstance(v, Job)]:
        status = jobs.runner.status(jobs.spec_of(job))
        if status["state"] in ("Running", "Stopping", "Failed", "Completed", "Has results", "Killed"):
            older = " (from a run of an older model)" if status["stale"] else ""
            for text in jobs.diagnose(jobs.spec_of(job)):
                add(nid, f"Convergence: {text}{older}")
    return dict(warn)


# --------------------------------------------------------------------------------------------- Abaqus' data check
def _messages(spec: jobs.JobSpec, check: bool = True) -> list[tuple[str, str]]:
    """(ERROR / WARNING, text) of a data check (or of the run), from its .dat and .msg (each message once)."""
    found: list[tuple[str, str]] = []
    for ext in (".dat", ".msg"):
        text = jobs.read_text(spec.check_file(ext) if check else spec.file(ext))
        for m in re.finditer(r"\*\*\*(ERROR|WARNING):(.*?)(?:\n\s*\n|\n\s*\*|\Z)", text, re.S):
            body = " ".join(m.group(2).split())
            if body and (m.group(1), body) not in found:
                found.append((m.group(1), body))
    return found


FAILED = re.compile(r"^NodeCode FEA: (.+?) failed: (.*)$")


NOTE = re.compile(r"^NodeCode FEA: (.+?) note: (.*)$")


def cae_failures(output: str, node_keys: dict[str, list[str]], pattern=FAILED) -> dict[str, list[str]]:
    """{node id: [what CAE said]} for the CAE objects a sync couldn't build ("instance M B failed: …"), on the
    nodes that make them. The ledger retries a failed object at every sync, so each sync reports them all.
    pattern=NOTE: what CAE reported about an object it built ("feature M P F note: …", e.g. a defeaturing's
    volume change); those come only when it is (re)built."""
    owner = {key.replace("|", " "): nid for nid, keys in node_keys.items() for key in keys}
    out: dict[str, list[str]] = defaultdict(list)
    for line in output.splitlines():
        m = pattern.match(line.strip())
        if m and m.group(1) in owner:
            text = f"In CAE: {m.group(2).strip() or 'failed'}"
            if text not in out[owner[m.group(1)]]:
                out[owner[m.group(1)]].append(text)
    return dict(out)


def datacheck_warnings(graph, results, node_inputs, entries, instances) -> dict[str, list[str]]:
    """Each data-check message on the node that made what it names; the rest on the Job."""
    out: dict[str, list[str]] = defaultdict(list)
    types = {n["id"]: n.get("type") for n in graph.get("nodes", [])}
    for job_nid, job in [(nid, v) for nid, v in entries if isinstance(v, Job)]:
        spec = jobs.spec_of(job)
        failed = jobs.runner.status(spec)["state"] == "Failed"
        if not jobs.runner.checked(spec) and not failed:
            continue
        model = job.model.name
        names: dict[str, set] = defaultdict(set)  # upper-case CAE name -> nodes that made it
        for nid, v in entries:
            owner = getattr(getattr(v, "model", None), "name", None) or getattr(
                getattr(getattr(v, "step", None), "model", None), "name", None)
            if isinstance(v, Instance):
                owner = v.part.model.name
            if owner != model or not getattr(v, "name", ""):
                continue
            if types.get(nid) in ("model.section",) and isinstance(v, Part):
                for section, _ in v.sections:
                    names[section.material.name.upper()].add(nid)
                    names[section.name.upper()].add(nid)
            elif not isinstance(v, Part) or types.get(nid) == "model.part":
                names[v.name.upper()].add(nid)
        gravity = {nid for nid, v in entries if isinstance(v, Condition) and v.kind == "Gravity"}
        missing_section = {nid for (m, _), (nid, inst) in instances.items()
                           if m == model and not final_part(inst.part, [p for _, p in entries if isinstance(p, Part)])
                           .sections}
        sources = []
        if jobs.runner.checked(spec):
            sources += [("Data check", m, jobs.runner.check_stale(spec)) for m in _messages(spec)]
        if failed:  # the run's own errors (zero pivot, too many attempts…); its warnings are often many and harmless
            sources += [("Run", m, jobs.runner.status(spec)["stale"]) for m in _messages(spec, check=False)
                        if m[0] == "ERROR"]
        for origin, (level, text), stale in sources:
            upper = text.upper()
            nodes = set()
            for name, nids in names.items():
                if re.search(r"(?<![A-Z0-9_-])" + re.escape(name) + r"(?![A-Z0-9_-])", upper):
                    nodes |= nids
            if "GRAV" in upper:
                nodes |= gravity
            if "MISSING PROPERTY" in upper or "ERRELEMMISSINGSECTION" in upper:
                nodes |= missing_section
            label = f"{origin} {'error' if level == 'ERROR' else 'warning'}: {text}"
            if stale:
                label += (" (from a check of an older model: press Check again)" if origin == "Data check" else
                          " (from a run of an older model)")
            for nid in nodes or {job_nid}:
                out[nid].append(label)
    return out
